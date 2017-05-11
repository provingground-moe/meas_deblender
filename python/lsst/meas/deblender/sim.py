from __future__ import print_function, division
from collections import OrderedDict
import logging

import numpy as np
import scipy.spatial
import matplotlib
import matplotlib.pyplot as plt
from astropy.table import Table as ApTable

import lsst.afw.table as afwTable
import lsst.afw.math as afwMath

from . import utils as debUtils
from . import baseline
from . import display

logging.basicConfig()
logger = logging.getLogger("lsst.meas.deblender")

def loadSimCatalog(filename):
    """Load a catalog of galaxies generated by galsim
    
    This can be used to ensure that the deblender is correctly deblending objects
    """
    cat = afwTable.BaseCatalog.readFits(filename)
    columns = []
    names = []
    for col in cat.getSchema().getNames():
        names.append(col)
        columns.append(cat.columns.get(col))
    simTable = ApTable(columns, names=tuple(names))
    return cat, simTable

def getNoise(calexps):
    """Get the median noise in each exposure
    
    Parameters
    ----------
    calexps: list of calexp's (`lsst.afw.image.imageLib.ExposureF`)
        List of calibrated exposures to estimate the noise
    
    Returns
    -------
    avgNoise: list of floats
        A list of the median value for all pixels in each ``calexp``.
    """
    avgNoise = []
    for calexp in calexps:
        var = calexp.getMaskedImage().getVariance()
        mask = calexp.getMaskedImage().getMask()
        stats = afwMath.makeStatistics(var, mask, afwMath.MEDIAN)
        avgNoise.append(np.sqrt(stats.getValue(afwMath.MEDIAN)))
    return avgNoise

def buildFootprintPeakTable(footprint, filters, sid=None):
    """Create a table of peak info to compare a single blend to simulated data
    
    Parameters
    ----------
    footprint: `lsst.meas.afw.detection.Footprint`
        Footprint containing the peak catalog.
    filters: list of strings
        Names of filters used for each flux measurement
    sid: int, default=``None``
        The source id from the `afw.table.SourceCatalog` that contains the footprint.

    Returns
    -------
    peakTable: `astropy.table.Table`
        Table with parent ID, peak index (in the parent), (x,y) coordinates, if the object is blended,
        peaks contained in the footprint, the parent footprint (containing the peak), and the flux in each
        filter.
    """
    # Keep track of blended sources
    if len(footprint.getPeaks())>=2:
        blended = True
    else:
        blended = False
    # Make a table of the peaks
    parents = []
    peakIdx = []
    peaks = []
    x = []
    y = []
    blends = []
    footprints = []
    if sid is None:
        sid = 0
    for pk, peak in enumerate(footprint.getPeaks()):
        parents.append(sid)
        peakIdx.append(pk)
        peaks.append(peak)
        x.append(peak.getIx())
        y.append(peak.getIy())
        blends.append(blended)
        footprints.append(footprint)
    # Create the peak Table
    peakTable = ApTable([parents, peakIdx, x, y, blends, peaks, footprints],
                        names=("parent", "peakIdx", "x", "y", "blended", "peak", "parent footprint"))
    # Create empty columns to hold fluxes
    for f in filters:
        peakTable["flux_"+f] = np.nan
    
    return peakTable

def buildPeakTable(expDb, filters):
    """Create a table of peak info to compare to simulated data
    
    Parameters
    ----------
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    filters: list of strings
        Names of filters used for each flux measurement
    
    Returns
    -------
    peakTable: `astropy.table.Table`
        Table with parent ID, peak index (in the parent), (x,y) coordinates, if the object is blended,
        peaks contained in the footprint, the parent footprint (containing the peak), and the flux in each
        filter.
    """
    parents = []
    peakIdx = []
    peaks = []
    x = []
    y = []
    blends = []
    footprints = []
    for src in expDb.mergedDet:
        sid = src.getId()
        footprint = src.getFootprint()
        if len(footprint.getPeaks())>=2:
            blended = True
        else:
            blended = False
        for pk, peak in enumerate(footprint.getPeaks()):
            parents.append(sid)
            peakIdx.append(pk)
            peaks.append(peak)
            x.append(peak.getIx())
            y.append(peak.getIy())
            blends.append(blended)
            footprints.append(footprint)
    peakTable = ApTable([parents, peakIdx, x, y, blends, peaks, footprints],
                        names=("parent", "peakIdx", "x", "y", "blended", "peak", "parent footprint"))
    # Create empty columns to hold fluxes
    for f in filters:
        peakTable["flux_"+f] = np.nan
    
    return peakTable

def matchToRef(peakTable, simTable, filters, maxSeparation=3, poolSize=-1, avgNoise=None,
               display=True, calexp=None, bbox=None):
    """Match a peakTable to the simulated Table
    
    Parameters
    ----------
    peakTable: `astropy.table.Table` returned from `buildPeakTable`
        Table with information about all of the peaks in an image, not just the parents
    simTable: `astropy.table.Table`
        The second object returned from `loadSimCatalog`, containing the true values of the simulated data.
    filters: list of strings
        Names of the filters in the ``peakTable`` and ``simTable``.
    maxSeparation: int, default=3
        Maximum separation distance (in pixels) between two sources to be considered a match
    poolSize: int, default=-1
        Number of processes to use in the kdTree search. ``poolSize=-1`` use the maximum number of
        available processors.
    avgNoise: list of floats, default=None
        Average noise for the image in each filter. If ``avgNoise`` is not ``None`` and
        ``display`` is ``True``, then the average noise in each image is plotted
    display: bool
        Whether or not to display plots of the matched data.
    calexp: `lsst.afw.image.imageLib.ExposureF`, default=``None``
        If ``display`` is True and ``calexp`` is not ``None``, the image is displayed with
        sources labeled.
    
    Returns
    -------
    matchTable: `astropy.table.Table`
        Table of 
    idx: `numpy.ndarray`
        Array of indices to match the simTable to the peakTable.
    unmatchedTable: `astropy.table.Table`
        Table of simulated sources not detected by the LSST stack.
        In addition to the columns in ``simTable``, there is also a
        column that lists the ratio of the ``flux/avgNoise`` in each
        band to help determine why certain sources are undetected.
    """
    # Create arrays that scipy.spatial.cKDTree can recognize and find matches for each peak
    peakCoords = np.array(list(zip(peakTable['x'], peakTable['y'])))
    simCoords = np.array(list(zip(simTable['x'], simTable['y'])))
    kdtree = scipy.spatial.cKDTree(simCoords)
    d2, idx = kdtree.query(peakCoords, n_jobs=poolSize)
    # Only consider matches less than the maximum separation
    matched = d2<maxSeparation
    # Check to see if any peaks are matched with the same reference source
    unique, uniqueInv, uniqueCounts = np.unique(idx[matched], return_inverse=True, return_counts=True)
    # Create a table with sim information matched to each peak
    matchTable = simTable[idx]
    matchTable["matched"] = matched
    matchTable["distance"] = d2
    matchTable["duplicate"] = False
    matchTable["duplicate"][matched] = uniqueCounts[uniqueInv]>1
    # Define zero values for unmatched sources
    emptyPatch = np.zeros_like(matchTable["intensity_"+filters[0]][0])
    emptySed = np.zeros_like(matchTable["sed"][0])
    # Zero out unmatched source data
    for fidx in filters:
        matchTable["intensity_"+fidx][~matched] = emptyPatch
        matchTable["sed"] = emptySed
        matchTable["flux_"+fidx][~matched] = 0.0
        matchTable["size"][~matched] = 0.0
        matchTable["redshift"][~matched] = 0.0
        matchTable["x"][~matched] = peakTable[~matched]["x"]
        matchTable["y"][~matched] = peakTable[~matched]["y"]

    # Display information about undetected sources
    sidx = set(idx[matched])
    srange = set(range(len(simTable)))
    unmatched = np.array(list(srange-sidx))
    logger.info("Sources not detected: {0}\n".format(len(unmatched)))

    # Store data for unmatched sources for later analysis
    unmatchedTable = simTable[unmatched]
    allRatios = OrderedDict([(f, []) for f in filters])
    for sidx in unmatched:
        ratios = []
        for fidx,f in enumerate(filters):
            ratio = np.max(simTable[sidx]["intensity_"+f])/avgNoise[fidx]
            ratios.append(ratio)
            allRatios[f].append(ratio)
    for col, ratios in allRatios.items():
        unmatchedTable["{0} peak/noise".format(col)] = ratios
    
    # Display the unmatched sources ratios
    ratios = [f+" peak/noise" for f in filters]
    all_ratios = np.array(unmatchedTable[ratios]).view(np.float64).reshape(len(unmatchedTable), len(ratios))
    max_ratios = np.max(all_ratios, axis=1)
    plt.plot(max_ratios, '.')
    plt.xlabel("Source number")
    plt.ylabel("peak flux/noise")
    plt.xlim([-1, len(max_ratios)])
    plt.title("Unmatched")
    plt.show()

    if display:
        x = range(len(filters))
    
        for src in simTable[unmatched]:
            flux = np.array([src["flux_{0}".format(f)] for f in filters])
            plt.plot(x, flux, '.-', c="#4c72b0")
        plt.plot(x, flux, '.-', c="#4c72b0", label="Not Detected")
        if avgNoise is not None:
            plt.plot(x, avgNoise, '.-', c="#c44e52", label="Background")
        plt.legend(loc='center left', bbox_to_anchor=(1, .5),
                   fancybox=True, shadow=True)
        plt.xticks([-.25]+x+[x[-1]+.25], [""]+[f for f in filters]+[""])
        plt.xlabel("Filter")
        plt.ylabel("Total Flux")
        plt.show()

        if calexp is not None:

            unmatched = peakTable[~matchTable["matched"]]
            unmatchedParents = np.unique(unmatched["parent"])

            for pid in unmatchedParents:
                footprint = peakTable[peakTable["parent"]==pid][0]["parent footprint"]
                bbox = footprint.getBBox()
                img = debUtils.extractImage(calexp.getMaskedImage(), bbox)
                vmin, vmax = debUtils.zscale(img)
                plt.imshow(img, vmin=vmin, vmax=10*vmax)
                xmin = bbox.getMinX()
                ymin = bbox.getMinY()
                xmax = xmin+bbox.getWidth()
                ymax = ymin+bbox.getHeight()

                peakCuts = ((peakTable["x"]>xmin) &
                           (peakTable["x"]<xmax) &
                           (peakTable["y"]>ymin) &
                           (peakTable["y"]<ymax))
                goodX = peakTable["x"][peakCuts & matchTable["matched"]]
                goodY = peakTable["y"][peakCuts & matchTable["matched"]]
                badX = peakTable["x"][peakCuts & ~matchTable["matched"]]
                badY = peakTable["y"][peakCuts & ~matchTable["matched"]]
                plt.plot(goodX-xmin, goodY-ymin, 'gx', mew=2)

                simCuts = ((simTable['x']>=xmin) &
                           (simTable['x']<=xmax) &
                           (simTable['y']>=ymin) &
                           (simTable['y']<=ymax))
                simx = simTable['x'][simCuts]-xmin
                simy = simTable['y'][simCuts]-ymin
                plt.plot(simx, simy, 'o', ms=20, mec='c', mfc='none')

                plt.plot(badX-xmin, badY-ymin, 'rx', mew=2)
                plt.xlim([0, bbox.getWidth()])
                plt.ylim([0, bbox.getHeight()])
                plt.show()
    return matchTable, idx, unmatchedTable

def deblendSimExposuresOld(filters, expDb, peakTable=None):
    """Use the old deblender to deblend an image

    Parameters
    ----------
    filters: list of strings
        Names of filters used for each flux measurement
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    peakTable: `astropy.table.Table` returned from `buildPeakTable`
        Table with information about all of the peaks in an image, not just the parents

    Result
    ------
    deblenderResults: OrderedDict of `lsst.meas.deblender.baseline.DeblenderResult`s
        Dictionary of results obtained by running the old deblender on all of the footprints in ``expDb``.
    """
    plugins = baseline.DEFAULT_PLUGINS
    maskedImages = [calexp.getMaskedImage() for calexp in expDb.calexps]
    psfs = [calexp.getPsf() for calexp in expDb.calexps]
    fwhm = [psf.computeShape().getDeterminantRadius() * 2.35 for psf in psfs]
    blends = expDb.mergedTable["peaks"]>1
    deblenderResults = OrderedDict()
    parents = OrderedDict()
    for n, blend in enumerate(expDb.mergedDet[blends]):
        parents[blend.getId()] = blend
        logger.debug("Deblending blend {0}".format(n))
        footprint = blend.getFootprint()
        footprints = [footprint]*len(expDb.calexps)
        deblenderResult = baseline.newDeblend(plugins, footprints, maskedImages, psfs, fwhm, filters=filters)
        deblenderResults[blend.getId()] = deblenderResult
    
        if peakTable is not None:
            for p,peak in enumerate(deblenderResult.peaks):
                cuts = (peakTable["parent"]==blend.getId()) & (peakTable["peakIdx"]==p)
                for f in filters:
                    fluxPortion = peak.deblendedPeaks[f].fluxPortion.getImage().getArray()
                    peakTable["flux_"+f][cuts] = np.sum(fluxPortion)

    return deblenderResults

def displayImage(src, ratio, fidx, expDb):
    """Display an Image

    Called from `calculateIsolatedFlux` when an isolated source has an unusually large difference
    from the simulated data.

    Parameters
    ----------
    parent: int
        Parent ID (in SourceCatalog)
    ratio: int
        ``100 *`` ``real flux``/``simulated flux`` for the source
    fidx: string
        Index of the filter to use for displaying the image
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.

    Returns
    -------
    None
    """
    mask = debUtils.getFootprintArray(src)[1].mask
    if hasattr(src, 'getFootprint'):
        src = src.getFootprint()
    img = debUtils.extractImage(expDb.calexps[fidx].getMaskedImage(), src.getBBox())
    img = np.ma.array(img, mask=mask)
    plt.imshow(img)
    plt.title("Flux Difference: {0}%".format(ratio))
    plt.show()

def calculateNmfFlux(expDb, peakTable):
    """Calculate the flux for each object in a peakTable

    Parameters
    ----------
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    peakTable: `astropy.table.Table` returned from `buildPeakTable`
        Table with information about all of the peaks in an image, not just the parents

    Returns
    -------
    None, the ``peakTable`` is modified in place.
    """
    for pk, peak in enumerate(peakTable):
        if peak["parent"] in expDb.deblendedParents:
            deblendedParent = expDb.deblendedParents[peak["parent"]]
            for fidx, f in enumerate(expDb.filters):
                template = deblendedParent.getTemplate(fidx, peak["peakIdx"])
                peakTable["flux_"+f][pk] = np.sum(template)

def calculateIsolatedFlux(filters, expDb, peakTable, simTable, avgNoise, fluxThresh=2, fluxRatio=0.5):
    """Calculate the flux of all isolated sources
    
    Get the flux for all of the sources in a ``peakTable`` not in a blend.
    if the ratio of flux/simFlux or simFlux/flux is low,
    and the total flux is above ``fluxThresh``, the source is displayed to
    determine the inconsistency.

    Parameters
    ----------
    filters: list of strings
        Names of filters used for each flux measurement.
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    peakTable: `astropy.table.Table` returned from `buildPeakTable`
        Table with information about all of the peaks in an image, not just the parents
    simTable: `astropy.table.Table`
        Result of ``match2Ref``, which matches the ``peakTable`` and simulated table.
    fluxThresh: int, default=100
        Minimum amount of flux an object must have to be flag a discrepancy in measured vs simulated flux.

    Returns
    -------
    None, this function updates the ``peakTable`` in place.
    """
    for n, peak in enumerate(peakTable):
        if peak["blended"] or ~simTable[n]["matched"]:
            continue
        footprint = peak["parent footprint"]
        mask = debUtils.getFootprintArray(footprint)[1].mask
        
        for fidx, f in enumerate(filters):
            img = debUtils.extractImage(expDb.calexps[fidx].getMaskedImage(), footprint.getBBox())
            img = np.ma.array(img, mask=mask)
            flux = np.ma.sum(img)
            peakTable["flux_"+f][n] = flux
            
            simFlux = simTable["flux_{0}".format(f)][n]
            maxFlux = np.max(simTable["intensity_{0}".format(f)][n])
            # Display any sources with very large flux differences
            if np.abs(flux-simFlux)/simFlux>.5 and maxFlux/avgNoise[fidx]>fluxThresh:
                logger.info("n: {0}, Filter: {1}, simFlux: {2}, max flux: {3}, total flux: {4}".format(
                    n, f, simFlux, maxFlux, flux))
                displayImage(peak["parent footprint"], int(100*np.abs(flux-simFlux)/simFlux), fidx, expDb)

def calculateFluxPortion(expDb, peakTable):
    """Calculate the flux portion for NMF deblends

    Parameters
    ----------
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    peakTable: `astropy.table.Table` returned from `buildPeakTable`
        Table with information about all of the peaks in an image, not just the parents

    Returns
    -------
    None, this function updates the ``peakTable`` in place.
    """
    for pk, peak in enumerate(peakTable):
        if peak["parent"] in expDb.deblendedParents:
            deblendedParent = expDb.deblendedParents[peak["parent"]]
            if deblendedParent.peakFlux is None:
                deblendedParent.getFluxPortion()
            for fidx, f in enumerate(expDb.filters):
                peakTable["flux_"+f][pk] = deblendedParent.peakFlux[fidx][peak["peakIdx"]]

def calculateSedsFromFlux(tbl, filters, inPlace=True):
    """Calculate the SED for each source

    For each unblended source, use the flux in each band to calculate the normalized (to one) SED
    for each source (row) in ``tbl``.

    Parameters
    ----------
    tbl: `astropy.table.Table`
        Table with flux measurements.
    filters: list of strings
        Names of filters used for each flux measurement.
    inPlace: bool, default = ``True``
        Whether or not to update the ``"sed"`` column in ``tbl`` in place,
        or just return the value.

    Returns
    -------
    seds: `numpy.ndarray`
        List of seds for each row in ``tbl``.
    normalization: `numpy.ndarry`
        Normalization constant for each sources (row) SED, used to normalize the SED to one.
    """
    fluxCols = ["flux_"+f for f in filters]
    shape = (len(tbl), len(fluxCols))
    seds = tbl[fluxCols].as_array().view(np.float64).reshape(shape)
    normalization = np.sum(seds, axis=1)
    seds = seds/normalization[:,None]
    if inPlace:
        tbl["sed"] = seds
    return seds, normalization

def plotSedComparison(simTable, simSeds, deblendedTables, minFlux):
    """Compare the SED from simulated data and various flux calculations

    Using the results from a set of deblender results
    (for example the new deblender templates, re-apportioned flux using the new deblender templates,
    old deblender results, etc.), compare the SED's calculated using each method with the
    simulated results.

    Parameters
    ----------
    simTable: `astropy.table.Table`
        The second object returned from `loadSimCatalog`, containing the true values of the simulated data.
    simSeds: list of floats
        SED or each source in ``simTable``.
    deblendedTables: dict of `astropy.table.Table`
        Dictionary with results using different deblending methods, where the keys of ``deblendedTables``
        are the labels for each tables SED results in the final plot

    Returns
    -------
    None
    """
    matched = simTable["matched"]
    sed = simSeds[matched]
    goodFlux = simTable["flux_i"][matched]>minFlux
    flux = simTable["flux_i"][matched]
    
    diffs = OrderedDict()
    errors = OrderedDict()
    goodErrors = OrderedDict()
    for tblName, tbl in deblendedTables.items():
        diff = tbl["sed"][matched]-sed
        diffs[tblName] = diff
        errors[tblName] = np.sqrt(np.sum(((diff/sed)**2), axis=1)/len(sed[0]))
        goodErrors[tblName] = errors[tblName][goodFlux]
    
    # Plot the histogram
    plt.figure(figsize=(8,4))
    bins = np.arange(0,23,2)
    bins = [0,5,10,15,20,25]
    #weight = np.ones_like(errOld[goodFlux])/len(errOld[goodFlux])
    #clippedErrors = [np.clip(err*100, bins[0], bins[-1]) for err in [errOld[goodFlux], errNmf[goodFlux]]]
    #plt.hist(clippedErrors, bins=bins, weights=[weight]*2, label=["LSST","NMF"])
    
    weight = np.ones_like(errors[errors.keys()[0]][goodFlux])/len(errors[errors.keys()[0]][goodFlux])
    clippedErrors = [np.clip(err*100, bins[0], bins[-1]) for err in goodErrors.values()]
    plt.hist(clippedErrors, bins=bins, weights=[weight]*len(deblendedTables), label=deblendedTables.keys())
    
    xlabels = [str(b) for b in bins[:-1]]
    xlabels[-1] += "+"
    plt.xticks(bins, xlabels)
    plt.title("SED")
    plt.xlabel("Error (%)")
    plt.ylabel("Fraction of Sources")
    plt.grid()
    plt.legend(loc="center left", fancybox=True, shadow=True, ncol=1, bbox_to_anchor=(1, 0.5))
    plt.show()

    # Setup the combined SED scatter plot with all sources included
    #fig = plt.figure(figsize=(8,3))
    #ax = fig.add_subplot(1,1,1)
    #ax.set_frame_on(False)
    #ax.set_xticks([])
    #ax.get_yaxis().set_visible(False)
    #ax.set_xlabel("Simulated Flux", labelpad=30)

    # Plot the SED scatter plots for LSST and NMF deblending
    #ax = fig.add_subplot(1,2,1)
    #ax.plot(flux[goodFlux], errOld[goodFlux], '.', label="LSST")
    #ax.plot(flux[~goodFlux], errOld[~goodFlux], '.', label="Bad LSST")
    #ax.set_ylabel("Fractional Error")
    #ax.set_title("LSST", y=.85)
    #ax = fig.add_subplot(1,2,2)
    #ax.plot(flux[goodFlux], errNmf[goodFlux], '.', label="NMF")
    #ax.plot(flux[~goodFlux], errNmf[~goodFlux], '.', label="Bad NMF")
    #ax.set_title("NMF", y=.85)
    #plt.show()

    # Plot the clipped SED scatter plots
    plt.figure(figsize=(8,5))
    markers = ['.','+','x']
    while len(markers)<len(deblendedTables.keys()):
        markers = markers * 2
    for n, (tblName, err) in enumerate(goodErrors.items()):
        plt.plot(flux[goodFlux], err, markers[n], label=tblName, alpha=.5)
    plt.xlabel("Simulated Flux")
    plt.ylabel("Fractional Error")
    plt.legend(loc="center left", fancybox=True, shadow=True, ncol=1, bbox_to_anchor=(1, 0.5))
    plt.gca().yaxis.grid(True)
    plt.show()

def compareMeasToSim(simTables, deblendedTblDict, filters, minFlux=50):
    """Compare deblended fluxes to simulated data

    Using the results from a set of deblender results
    (for example the new deblender templates, re-apportioned flux using the new deblender templates,
    old deblender results, etc.), compare the flux calculated using each method with the simulated
    results.

    Parameters
    ----------
    simTable: `astropy.table.Table`
        The second object returned from `loadSimCatalog`, containing the true values of the simulated data.
    deblendedTblDict: dict of list of `astropy.table.Table`
        Dictionary with results using different deblending methods, where the keys of ``deblendedTables``
        are the labels for each tables SED results in the final plot and the values are a list of
        tables, since the data often comes from multiple images.
    filters: list of strings
        Names of filters used for each flux measurement.
    minFlux: float
        Minimum flux for a source needed to be included in the statistics.

    Returns
    -------
    deblendedTables: OrderedDict of `astropy.table.Table`
        Combined table of all blends
    """
    from astropy.table import vstack

    simTable = vstack(simTables)
    deblendedTables = OrderedDict()
    # Combine the peakTables in each exposure into a single table
    # (this is not a merge, and assumes that the exposure catalogs do not overlap)
    for tblName, tbls in deblendedTblDict.items():
        # Keep track of which exposure each blend belonged to
        for n in range(len(tbls)):
            deblendedTblDict[tblName][n]["image"] = n+1
        deblendedTables[tblName] = vstack(tbls)
    blended = deblendedTables[tblName]["blended"]
    matched = simTable["matched"]

    # Display statistics
    logger.info("Total Simulated Sources: {0}".format(len(simTable)))
    logger.info("Total Detected Sources: {0}".format(len(blended)))
    logger.info("Total Matches: {0}".format(np.sum(simTable["matched"])))
    logger.info("Matched Isolated sources: {0}".format(np.sum(simTable["matched"]&~blended)))
    logger.info("Matched Blended sources: {0}".format(np.sum(simTable["matched"]&blended)))
    logger.info("Total Duplicates: {0}".format(np.sum(simTable["duplicate"])))

    # Calculate and compare SEDs
    simSeds, normalization = calculateSedsFromFlux(simTable, filters, inPlace=False)
    for tblName in deblendedTables:
        calculateSedsFromFlux(deblendedTables[tblName], filters)
    plotSedComparison(simTable, simSeds, deblendedTables, minFlux)

    for f in filters:
        flux = "flux_"+f
        diff = OrderedDict()
        lowFlux = simTable[flux]<minFlux
        plt.figure(figsize=(8,4))
        differences = OrderedDict()
        for tblName, tbl in deblendedTables.items():
            diff = (tbl[flux]-simTable[flux])/simTable[flux]
            differences[tblName] = diff
            plt.plot(simTable[flux][matched & blended & ~lowFlux], diff[matched & blended & ~lowFlux], '.',
                     label=tblName)
        plt.plot(simTable[flux][matched & ~blended & ~lowFlux], diff[matched & ~blended & ~lowFlux], '.',
                 label="Isolated")
        plt.title("Filter {0}".format(f), y=.9)
        plt.xlabel("Simulated Flux (counts)")
        plt.ylabel("Fractional Error")
        #plt.legend(loc="upper center", fancybox=True, shadow=True, bbox_to_anchor=(.5, 1.2), ncol=3)
        plt.legend(loc="center left", fancybox=True, shadow=True, ncol=1, bbox_to_anchor=(1, 0.5))
        plt.gca().yaxis.grid(True)
        plt.show()
        
        plt.figure(figsize=(8,4))
        bins = np.arange(0,23,2)
        bins = [0,5,10,15,20,25]
        datasets = [np.abs(diff[matched&blended&~lowFlux]) for tblName, diff in differences.items()]
        diff = differences["LSST"]
        datasets = datasets + [np.abs(diff[matched&~blended&~lowFlux])]
        weights = [np.ones_like(data)/len(data) for data in datasets]
        clippedErrors = [np.clip(data*100, bins[0], bins[-1]) for data in datasets]
        plt.hist(clippedErrors, bins=bins, weights=weights, label=differences.keys()+["Isolated"])
        xlabels = [str(b) for b in bins[:-1]]
        xlabels[-1] += "+"
        plt.xticks(bins, xlabels)
        plt.title("Filter {0} Flux".format(f), y=.9)
        plt.xlabel("Error (%)")
        plt.ylabel("Fraction of Sources")
        plt.gca().yaxis.grid(True)
        plt.legend(loc="center left", fancybox=True, shadow=True, ncol=1, bbox_to_anchor=(1, 0.5))
        plt.show()
        
        #logger.info("Isolated Mean: {0}".format(np.mean(np.abs(diff[matched&~blended & ~lowflux]))))
        #logger.info("Isolated RMS: {0}".format(np.sqrt(np.mean(diff[matched&~blended & ~lowflux])**2+
        #                                               np.std(diff[matched&~blended & ~lowflux])**2)))
        #logger.info("Blended Mean: {0}".format(np.mean(np.abs(diff[matched&blended & ~lowflux]))))
        #logger.info("Blended RMS: {0}".format(np.sqrt(np.mean(diff[matched&blended & ~lowflux])**2+
        #                                              np.std(diff[matched&blended & ~lowflux])**2)))
    return deblendedTables

def checkForDegeneracy(expDb, minFlux=None, filterIdx=None):
    """Calculate the correlation for each pair of objects and store it in the parent deblends
    
    Parameters
    ----------
    expDb: `lsst.meas.deblender.proximal.ExposureDeblend`
        Object containing all blended objects and catalogs for a ``calexp``.
    minFlux: float, default = ``None``
        If ``filterIdx`` is not ``None`` and ``minFlux`` is not ``None``, this is the
        minimum flux needed for a footprint to be displayed
    filterIdx: string, default = ``None``
        Index of the filter to use for displaying the image.
        If ``filterIdx`` is ``None`` then the image is not displayed.
    """
    for parentIdx, parent in expDb.deblendedParents.items():
        logger.info("Parent {0}".format(parentIdx))
        vmin, vmax = debUtils.zscale(parent.data[1])
        plt.figure(figsize=(6,6))
        # Optionally show the image data underneath the footprints
        if filterIdx is not None:
            display.maskPlot(parent.data[filterIdx], vmin=vmin, vmax=10*vmax, show=False)

        totalFlux = parent.intensities
        totalFlux = np.sum(totalFlux.reshape(totalFlux.shape[0], totalFlux.shape[1]*totalFlux.shape[2]),
                                             axis=1)
        goodFlux = totalFlux>0
        
        # Plot the footprint for each object
        for n, pk in enumerate(parent.intensities):
            subset = pk>minFlux
            if goodFlux[n]:
                display.maskPlot(subset, subset==0, show=False, alpha=.2, cmap='cool')
        
        for n,pk in enumerate(parent.peakCoords):
            plt.annotate(n, xy=pk)
        px, py = np.array(parent.peakCoords).T
        plt.plot(px[goodFlux], py[goodFlux], 'k.')
        plt.plot(px[~goodFlux], py[~goodFlux], 'rx')
        
        plt.xlim([0,parent.data[1].shape[1]])
        plt.ylim([0,parent.data[1].shape[0]])
        plt.show()
    
        # Show the correlation matrix
        degenerateFlux = parent.getCorrelations(minFlux=minFlux)
        plt.imshow(np.ma.array(degenerateFlux, mask=degenerateFlux==0))
        plt.title("Correlation between peak templates")
        plt.colorbar()
        plt.show()
