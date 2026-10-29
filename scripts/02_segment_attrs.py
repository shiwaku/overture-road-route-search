"""segment ごとの集計用の属性表を作る（PLAN.md 段階 2 の下ごしらえ）

網羅率の比較と属性の埋まり具合の集計に使う。1 行 1 segment で、geometry は持たない（segment.parquet と id で結べる）。

- pref: 重心が入る都道府県（JP-01〜JP-47）。Overture divisions の region を使う。
  海に面する県は領海を含む範囲（maritime）、内陸の県は陸地（land）。どれにも入らなければ NULL
- length_m: 測地線長
- oneway: 車が片方向にしか通れない（access_restrictions に heading 付きの denied があり、mode の指定が無いか車を含む）
- is_link: ランプ・連絡路（subclass=link）
- network: OSM の路線（routes.network）から道路法の分類に寄せたもの。expressway（JP:E）/ national / prefectural / NULL
- has_*: 属性があるか

    uv run python scripts/02_segment_attrs.py
"""

import time
from pathlib import Path

import duckdb
import numpy as np
import shapely

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
RAW = BASE / 'data' / 'raw' / RELEASE
JP = BASE / 'data' / 'jp' / RELEASE
GRID = 0.25     # 都道府県の範囲をこの度数の格子で切ってから判定する（大きな多角形のままだと遅い）

c = duckdb.connect()
c.sql("LOAD spatial; SET geometry_always_xy=true")
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


el('都道府県の範囲を格子で切る')
regions = c.sql(f"""
    SELECT region, ST_AsWKB(geometry) FROM read_parquet('{RAW / 'divisions_region.parquet'}')
    WHERE class = 'maritime' OR region NOT IN (
        SELECT region FROM read_parquet('{RAW / 'divisions_region.parquet'}') WHERE class = 'maritime')
    ORDER BY region""").fetchall()
pieces, piece_pref = [], []
for region, wkb in regions:
    g = shapely.from_wkb(bytes(wkb))
    x0, y0, x1, y1 = g.bounds
    xs = np.arange(np.floor(x0 / GRID) * GRID, x1, GRID)
    ys = np.arange(np.floor(y0 / GRID) * GRID, y1, GRID)
    cells = [shapely.box(x, y, x + GRID, y + GRID) for x in xs for y in ys]
    for p in shapely.intersection(g, cells):
        if not p.is_empty:
            pieces.append(p)
            piece_pref.append(region)
pieces = np.array(pieces)
piece_pref = np.array(piece_pref)
tree = shapely.STRtree(pieces)
el(f'  {len(regions)} 都道府県 → {len(pieces):,} 片')

el('segment の重心と属性を読む')
c.sql(f"""CREATE TABLE a AS
    SELECT
        id, country, subtype, class, subclass,
        ST_Length_Spheroid(geometry) AS length_m,
        ST_X(ST_Centroid(geometry)) AS cx, ST_Y(ST_Centroid(geometry)) AS cy,
        coalesce(len(list_filter(access_restrictions, lambda r:
            r.access_type = 'denied' AND r."when".heading IS NOT NULL AND r."when".during IS NULL
            AND r."between" IS NULL AND r."when".vehicle IS NULL AND r."when".using IS NULL
            AND (r."when".mode IS NULL OR list_has_any(r."when".mode, ['vehicle', 'motor_vehicle', 'car'])))), 0) > 0 AS oneway,
        coalesce(subclass = 'link', false) AS is_link,
        CASE
            WHEN list_has_any(list_transform(routes, lambda r: r.network), ['JP:E']) THEN 'expressway'
            WHEN len(list_filter(routes, lambda r: r.network = 'JP:national')) > 0 THEN 'national'
            WHEN len(list_filter(routes, lambda r: r.network LIKE 'JP:prefectural%')) > 0 THEN 'prefectural'
        END AS network,
        names.primary IS NOT NULL AS has_name,
        len(speed_limits) > 0 AS has_speed,
        len(width_rules) > 0 AS has_width,
        len(road_surface) > 0 AS has_surface,
        len(access_restrictions) > 0 AS has_access,
        len(level_rules) > 0 AS has_level,
        len(routes) > 0 AS has_route
    FROM read_parquet('{JP / 'segment.parquet'}')""")
cx, cy = c.sql('SELECT cx, cy FROM a').fetchnumpy().values()
el(f'  {len(cx):,} 本')

el('重心がどの都道府県に入るか')
pts = shapely.points(cx, cy)
idx_pt, idx_piece = tree.query(pts, predicate='within')
pref = np.full(len(pts), None, dtype=object)
pref[idx_pt[::-1]] = piece_pref[idx_piece[::-1]]   # 境界上で 2 つに入るときは先に見つかった方
el(f'  都道府県が決まらない: {sum(p is None for p in pref):,} 本')

import pyarrow as pa
c.register('p', pa.table({'pref': pa.array(pref, pa.string())}))
c.sql(f"""COPY (
    SELECT a.* EXCLUDE (cx, cy), p.pref
    FROM a POSITIONAL JOIN p
) TO '{JP / 'segment_attrs.parquet'}' (FORMAT parquet, COMPRESSION zstd)""")
el(f'完了 → {(JP / "segment_attrs.parquet").relative_to(BASE)}')
