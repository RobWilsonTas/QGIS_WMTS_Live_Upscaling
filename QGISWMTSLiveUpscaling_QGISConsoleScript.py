import os, tempfile, requests, subprocess
from math import floor
from pathlib import Path
from threading import Lock
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from itertools import product

from PIL import Image
from qgis.core import (QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRasterLayer, QgsApplication)
from qgis.PyQt.QtCore import QObject, pyqtSignal, QRunnable, QThreadPool, QTimer, Qt

#Make sure the super resolution module is available
#Ddddsr works without excessive dependencies
try:
    import ddddsr
except ModuleNotFoundError:
    pythonExe = str(Path(QgsApplication.prefixPath()).parent.parent) + r"\apps\Python312\python.exe"
    subprocess.run([pythonExe, "-m", "pip", "install", "ddddsr"], check=True)
    import ddddsr

#The actual upscaling
def runSuperResolution(inputImagePath, outputImagePath):
    try:
        ddddsr.SR("waifu2x_photo")(inputImagePath, outputImagePath)
    except Exception as e:
        print("SR failed:", e)

#Temp folders for holding the pngs
originalTilesFolder = tempfile.mkdtemp()
upscaledTilesFolder = tempfile.mkdtemp()

#Parameter config
tileSize = 256
webMercatorTopLeftX = -20037508.34278925
webMercatorTopLeftY = 20037508.34278925
initialTileResolution = 2 * 20037508.34278925 / tileSize

#Your wmts source
tileUrlTemplate = ("https://whatever.com/arcgis/rest/services/Basemaps/"
    "Topographic/MapServer/WMTS/tile/1.0.0/Basemaps_Topographic/default/"
    "GoogleMapsCompatible/{TileMatrix}/{TileRow}/{TileCol}.png")

#These are the scales for all the zoom levels of tile layers
zoomLevelScales = [559082264.0287178, 279541132.0143589, 139770566.0071794, 69885283.00358972,
    34942641.50179486, 17471320.75089743, 8735660.375448715, 4367830.187724357,
    2183915.093862179, 1091957.546931089, 545978.7734655447, 272989.3867327723,
    136494.6933663862, 68247.34668319309, 34123.67334159654, 17061.83667079827,
    8530.918335399136, 4265.459167699568, 2132.729583849784]

#Creating a QGIS layer from the source
xyzLayerUrl = "file:///" + upscaledTilesFolder + "/{z}/{x}/{y}.png"
liveRasterLayer = QgsRasterLayer("type=xyz&url=" + xyzLayerUrl, "List Topo Base 2x Scale", "wms")
QgsProject.instance().addMapLayer(liveRasterLayer)

#Making some multithreading stuff to not lag anything
threadPool = QThreadPool.globalInstance()
threadPool.setMaxThreadCount(2)
jobLock = Lock()
currentJobId = 0

httpSession = requests.Session()
httpAdapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
httpSession.mount("http://", httpAdapter)
httpSession.mount("https://", httpAdapter)

webMercatorCrs = QgsCoordinateReferenceSystem("EPSG:3857")

class Signals(QObject):
    tileReady = pyqtSignal()

signals = Signals()
refreshTimer = QTimer()
refreshTimer.setSingleShot(True)

def refreshLayer():
    liveRasterLayer.dataProvider().reloadData()
    liveRasterLayer.triggerRepaint()

refreshTimer.timeout.connect(refreshLayer, Qt.QueuedConnection)
signals.tileReady.connect(lambda: refreshTimer.start(150), Qt.QueuedConnection)

def findZoom(scale):
    return next((i for i, s in enumerate(zoomLevelScales) if scale >= s), len(zoomLevelScales) - 1)

#Figure out which column and row the tile is in
def coordinatesToTile(x, y, zoomLevel):
    res = initialTileResolution / (2 ** zoomLevel)
    col = floor((x - webMercatorTopLeftX) / (tileSize * res))
    row = floor((webMercatorTopLeftY - y) / (tileSize * res))
    return col, row

#Turn the column and row and whatnot into a folder and file name for the png
def tilePath(folderPath, zoomLevel, col, row):
    return os.path.join(folderPath, str(zoomLevel), str(col), str(row) + ".png")

#Tile cache so like we don't have to download the same png multiple times
downloadCache = {}

@lru_cache(maxsize=512)
def loadTile(path):
    return Image.open(path)

#Downloading the actual tile we want
def downloadTile(zoomLevel, col, row):
    key = (zoomLevel, col, row)
    if key in downloadCache:
        return downloadCache[key]

    localPath = tilePath(originalTilesFolder, zoomLevel, col, row)
    if os.path.exists(localPath):
        downloadCache[key] = localPath
        return localPath

    url = tileUrlTemplate.format(TileMatrix=zoomLevel, TileRow=row, TileCol=col)
    try:
        response = httpSession.get(url, timeout=10)
        response.raise_for_status()
        os.makedirs(os.path.dirname(localPath), exist_ok=True)
        with open(localPath, "wb") as fileHandle:
            fileHandle.write(response.content)
        downloadCache[key] = localPath
        return localPath
    except Exception as e:
        print("Download failed:", e)
        return None

class CanvasSRProcessor(QRunnable):
    def __init__(self, jobId, zoomLevel, minCol, minRow, maxCol, maxRow):
        super().__init__()
        self.jobId = jobId
        self.zoomLevel = zoomLevel
        self.minCol = minCol
        self.minRow = minRow
        self.maxCol = maxCol
        self.maxRow = maxRow

    def isCancelled(self):
        return self.jobId != currentJobId

    def run(self):
        if self.zoomLevel <= 0:
            return

        parentZoom = self.zoomLevel - 1
        parentMinCol, parentMaxCol = self.minCol // 2, self.maxCol // 2
        parentMinRow, parentMaxRow = self.minRow // 2, self.maxRow // 2
        width, height = parentMaxCol - parentMinCol + 1, parentMaxRow - parentMinRow + 1

        #Get like all the tiles we want
        parentCoords = list(product(range(parentMinCol, parentMaxCol + 1), range(parentMinRow, parentMaxRow + 1)))
        with ThreadPoolExecutor(max_workers=4) as executor:
            downloadedParentTiles = {}
            for coords, path in zip(parentCoords, executor.map(lambda coords: downloadTile(parentZoom, coords[0], coords[1]), parentCoords)):
                if path:
                    downloadedParentTiles[coords] = path

        if not downloadedParentTiles or self.isCancelled():
            return

        #Turn on all the times into one image
        combinedImage = Image.new("RGB", (width * tileSize, height * tileSize))
        for xOffset, col in enumerate(range(parentMinCol, parentMaxCol + 1)):
            for yOffset, row in enumerate(range(parentMinRow, parentMaxRow + 1)):
                if self.isCancelled():
                    return
                tileFilePath = downloadedParentTiles.get((col, row))
                if tileFilePath:
                    combinedImage.paste(loadTile(tileFilePath), (xOffset * tileSize, yOffset * tileSize))

        #Run the super resolution on the full images, rather than just the tiles
        tempInputPath = tilePath(originalTilesFolder, parentZoom, parentMinCol, parentMinRow).replace(str(parentMinRow) + ".png", "sr_" + str(parentZoom) + "_" + str(parentMinCol) + "_" + str(parentMinRow) + "_" + str(parentMaxCol) + "_" + str(parentMaxRow) + ".png")
        tempOutputPath = tempInputPath.replace(".png", "_sr.png")
        if not os.path.exists(tempOutputPath):
            combinedImage.save(tempInputPath)
            runSuperResolution(tempInputPath, tempOutputPath)

        if self.isCancelled():
            return

        #Slice up the image back into tiles so the layer knows where to find the tiles based on row and column
        srImage = Image.open(tempOutputPath)
        for xOffset in range(width * 2):
            for yOffset in range(height * 2):
                if self.isCancelled():
                    return
                crop = srImage.crop((xOffset * tileSize, yOffset * tileSize, (xOffset + 1) * tileSize, (yOffset + 1) * tileSize))
                childCol, childRow, childZoom = parentMinCol * 2 + xOffset, parentMinRow * 2 + yOffset, parentZoom + 1
                outputPath = tilePath(upscaledTilesFolder, childZoom, childCol, childRow)
                os.makedirs(os.path.dirname(outputPath), exist_ok=True)
                crop.save(outputPath)

        srImage.close()
        signals.tileReady.emit()

#This function is called whenever the user pans the canvas, so that way the tiles will appear where they're looking
def updateTiles():
    global currentJobId

    canvas = iface.mapCanvas()
    extent = canvas.extent()
    sourceCrs = canvas.mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(sourceCrs, webMercatorCrs, QgsProject.instance())
    webMercatorExtent = transform.transform(extent)

    widthMeters = webMercatorExtent.width()
    pixelWidth = canvas.size().width()
    scale = widthMeters / pixelWidth * 96 / 0.0254

    zoomLevel = findZoom(scale)
    minCol, maxRow = coordinatesToTile(webMercatorExtent.xMinimum(), webMercatorExtent.yMinimum(), zoomLevel)
    maxCol, minRow = coordinatesToTile(webMercatorExtent.xMaximum(), webMercatorExtent.yMaximum(), zoomLevel)

    with jobLock:
        currentJobId += 1
        jobId = currentJobId

    threadPool.start(CanvasSRProcessor(jobId, zoomLevel, minCol, minRow, maxCol, maxRow))

#Call the tiles to update once, then make it so that when the canvas moves the tiles refresh
updateTiles()
iface.mapCanvas().extentsChanged.connect(updateTiles)

#If you remove the tiles layer then it stops trying to update tiles
def onLayerRemoved(layerId):
    if liveRasterLayer.id() == layerId:
        iface.mapCanvas().extentsChanged.disconnect(updateTiles)
        QgsProject.instance().layerWillBeRemoved.disconnect(onLayerRemoved)

QgsProject.instance().layerWillBeRemoved.connect(onLayerRemoved)
