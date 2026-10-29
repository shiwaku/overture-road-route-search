"""06_view_gpkg.py の GeoPackage を class ごとに色分けした QGIS プロジェクトを作る

- 背景: 地理院タイル（淡色地図）
- major: class ごとに色と太さを変える。全縮尺で表示
- minor: class ごとに色を変える。1:50,000 より拡大したときだけ表示（全国を一度に描くと重い）

QGIS 同梱の Python で動かす:

    "C:\\Program Files\\QGIS 4.0.0\\bin\\python-qgis.bat" scripts/07_view_project.py
"""

from pathlib import Path

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
                       QgsLineSymbol, QgsProject, QgsRasterLayer, QgsRectangle, QgsReferencedRectangle,
                       QgsRendererCategory, QgsVectorLayer)

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
GPKG = BASE / 'data' / 'view' / RELEASE / 'roads_jp.gpkg'
QGZ = BASE / 'data' / 'view' / RELEASE / 'roads_jp.qgz'
MINOR_MIN_SCALE = 50_000

# class → (色, 太さ mm)
MAJOR = {
    'motorway': ('#d6336c', 0.9),
    'trunk': ('#e8590c', 0.8),
    'primary': ('#f59f00', 0.6),
    'secondary': ('#2f9e44', 0.5),
    'tertiary': ('#1c7ed6', 0.4),
}
MINOR = {
    'residential': ('#495057', 0.25),
    'unclassified': ('#868e96', 0.25),
    'living_street': ('#495057', 0.25),
    'service': ('#adb5bd', 0.2),
    'track': ('#a0522d', 0.25),
    'footway': ('#7048e8', 0.2),
    'pedestrian': ('#7048e8', 0.3),
    'steps': ('#7048e8', 0.3),
    'path': ('#9c36b5', 0.2),
    'cycleway': ('#0ca678', 0.3),
    'bridleway': ('#9c36b5', 0.2),
    'unknown': ('#ced4da', 0.2),
}

app = QgsApplication([], False)
app.initQgis()
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem('EPSG:3857'))

base = QgsRasterLayer('type=xyz&url=https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png&zmax=18&zmin=2',
                      '地理院タイル（淡色地図）', 'wms')
project.addMapLayer(base)


def add(layer_name, styles, title, min_scale=None):
    lyr = QgsVectorLayer(f'{GPKG}|layername={layer_name}', title, 'ogr')
    assert lyr.isValid(), layer_name
    cats = []
    for cls, (color, width) in styles.items():
        sym = QgsLineSymbol.createSimple({'line_color': color, 'line_width': str(width), 'capstyle': 'round'})
        cats.append(QgsRendererCategory(cls, sym, cls))
    lyr.setRenderer(QgsCategorizedSymbolRenderer('class', cats))
    if min_scale:
        lyr.setScaleBasedVisibility(True)
        lyr.setMinimumScale(min_scale)
    project.addMapLayer(lyr)
    return lyr


add('minor', MINOR, f'Overture 道路（細い道・1:{MINOR_MIN_SCALE:,} より拡大で表示）', MINOR_MIN_SCALE)
add('major', MAJOR, 'Overture 道路（幹線）')

# 最初は日本全域を表示する
project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(
    QgsRectangle(122.9, 24.0, 146.0, 45.6), QgsCoordinateReferenceSystem('EPSG:4326')))
project.write(str(QGZ))
print(f'→ {QGZ}')
app.exitQgis()
