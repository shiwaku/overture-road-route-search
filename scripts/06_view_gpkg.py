"""日本全域の Overture の道路を QGIS で見るための GeoPackage を作る

1,300 万本をそのまま開くと重いので、表示に要る列だけに絞り、2 つのレイヤに分ける。
QGIS では minor を拡大したときだけ表示する（プロジェクトは 07_view_project.py）。

- major: motorway〜tertiary（ランプ・連絡路を含む）
- minor: それ以外の道路（residential・unclassified・service・track・footway・path など）

列: id / class / subclass / name / oneway / max_speed（条件の無い最高速度 km/h）/ network / pref / length_m

    uv run python scripts/06_view_gpkg.py
"""

import subprocess
import time
from pathlib import Path

import duckdb

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
JP = BASE / 'data' / 'jp' / RELEASE
OUT = BASE / 'data' / 'view' / RELEASE
OGR2OGR = r'C:\Program Files\QGIS 4.0.0\bin\ogr2ogr.exe'
MAJOR = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')

OUT.mkdir(parents=True, exist_ok=True)
c = duckdb.connect()
c.sql("LOAD spatial; SET geometry_always_xy=true")
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


gpkg = OUT / 'roads_jp.gpkg'
tmp = gpkg.with_suffix('.tmp.gpkg')
tmp.unlink(missing_ok=True)
for layer, cond in (('major', f'IN {MAJOR}'), ('minor', f'NOT IN {MAJOR}')):
    pq = OUT / f'{layer}.parquet'
    el(f'{layer}: 列を絞って書き出す（Hilbert 順に並べる）')
    c.sql(f"""COPY (
        SELECT s.id, s.class, s.subclass, s.names.primary AS name, a.oneway,
            list_filter(s.speed_limits, lambda r: r.max_speed.unit = 'km/h' AND r."when" IS NULL AND r."between" IS NULL)[1].max_speed.value AS max_speed,
            a.network, a.pref, round(a.length_m, 1) AS length_m, s.geometry
        FROM read_parquet('{JP / 'segment.parquet'}') s
        JOIN read_parquet('{JP / 'segment_attrs.parquet'}') a USING (id)
        WHERE s.subtype = 'road' AND s.country = 'JP' AND s.class {cond}
        ORDER BY ST_Hilbert(s.geometry, {{'min_x': 122.0, 'min_y': 20.0, 'max_x': 154.0, 'max_y': 46.0}}::BOX_2D)
    ) TO '{pq}' (FORMAT parquet, COMPRESSION zstd)""")
    el(f'  {c.sql(f"SELECT count(*) FROM read_parquet(\'{pq}\')").fetchall()[0][0]:,} 本 → GeoPackage へ')
    subprocess.run([OGR2OGR, '-f', 'GPKG', str(tmp), str(pq), '-nln', layer, '-nlt', 'LINESTRING',
                    '-a_srs', 'EPSG:4326', '-lco', 'SPATIAL_INDEX=YES', '-gt', '200000',
                    *(['-update'] if tmp.exists() else [])], check=True)
    pq.unlink()
tmp.replace(gpkg)
el(f'完了 → {gpkg.relative_to(BASE)}  {gpkg.stat().st_size / 1e9:.2f} GB')
