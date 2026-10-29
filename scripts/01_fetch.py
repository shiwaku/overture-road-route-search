"""Overture の道路データ（segment / connector）と日本の範囲を取得し、日本の範囲で切り抜く（PLAN.md 段階 1）

1. 日本と係争地（北方領土・竹島・尖閣諸島）の範囲を divisions から取る
   切り抜きには領海を含む範囲（class=maritime）を使う。陸地（class=land）だと海の上の橋（アクアライン・瀬戸大橋など）が落ちる
2. 日本を囲む四角形で segment / connector を取る（S3 から。四角形には中国東北部・朝鮮半島・サハリンも入る）
3. segment の重心がどの範囲に入るかで `country` を付け、どれにも入らないものは捨てる
   北方領土などは消さずに残す（XA 歯舞・XB 竹島・XD 尖閣・XI 択捉・XO 色丹・XU 国後）。使うかどうかは後の段階で決める
4. connector は、残った segment から参照されているものだけ残す

    uv run python scripts/01_fetch.py            # 全部
    uv run python scripts/01_fetch.py --skip-download   # 2 の S3 からの取得を飛ばす（data/raw にある前提）
"""

import argparse
import time
from pathlib import Path

import duckdb

RELEASE = '2026-10-21.0'
S3 = f's3://overturemaps-us-west-2/release/{RELEASE}'
BBOX = (122.0, 20.0, 154.0, 46.0)        # 西, 南, 東, 北
COUNTRIES = ('JP', 'XA', 'XB', 'XD', 'XI', 'XO', 'XU')

BASE = Path(__file__).resolve().parent.parent
RAW = BASE / 'data' / 'raw' / RELEASE
OUT = BASE / 'data' / 'jp' / RELEASE

ap = argparse.ArgumentParser()
ap.add_argument('--skip-download', action='store_true')
a = ap.parse_args()
RAW.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

c = duckdb.connect()
c.sql("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'; SET geometry_always_xy=true")
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


def count(path):
    # DuckDB 2.0 の relation には fetchone() が無い
    return c.sql(f"SELECT count(*) FROM read_parquet('{path}')").fetchall()[0][0]


w, s, e, n = BBOX
in_bbox = f'bbox.xmin < {e} AND bbox.xmax > {w} AND bbox.ymin < {n} AND bbox.ymax > {s}'
src = lambda typ: f"read_parquet('{S3}/theme={typ[0]}/type={typ[1]}/*', hive_partitioning=1)"

if not a.skip_download:
    # 取得済みのファイルは飛ばす（S3 からの取得は segment だけで 13 分かかる）。取り直すときはファイルを消す
    if not (RAW / 'divisions_country.parquet').exists():
        el('divisions: 日本と係争地の範囲')
        c.sql(f"""COPY (
            SELECT id, country, class, names.primary AS name, is_land, is_territorial, bbox, geometry
            FROM {src(('divisions', 'division_area'))}
            WHERE subtype = 'country' AND country IN {COUNTRIES}
        ) TO '{RAW / 'divisions_country.parquet'}' (FORMAT parquet, COMPRESSION zstd)""")

    for typ in ('segment', 'connector'):
        p = RAW / f'{typ}_bbox.parquet'
        if not p.exists():
            el(f'{typ}: 四角形 {BBOX} で取得')
            # theme / type はパーティション列なので出力から外す。途中で落ちると壊れたファイルが残るので一時名で書く
            tmp = p.with_suffix('.tmp')
            c.sql(f"""COPY (
                SELECT * EXCLUDE (theme, type) FROM {src(('transportation', typ))} WHERE {in_bbox}
            ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
            tmp.rename(p)
        el(f'  {typ}_bbox: {count(p):,} 件')

el('segment: 重心がどの範囲（領海を含む）に入るかで country を付ける')
c.sql(f"""CREATE TABLE area AS
    SELECT country, geometry FROM read_parquet('{RAW / 'divisions_country.parquet'}')
    WHERE class = 'maritime' OR country NOT IN (
        SELECT country FROM read_parquet('{RAW / 'divisions_country.parquet'}') WHERE class = 'maritime')""")
print(c.sql('SELECT country, count(*) AS polygons FROM area GROUP BY ALL ORDER BY country'))
c.sql(f"""COPY (
    WITH seg AS (SELECT *, ST_Centroid(geometry) AS _c FROM read_parquet('{RAW / 'segment_bbox.parquet'}'))
    SELECT seg.* EXCLUDE (_c), area.country
    FROM seg JOIN area ON ST_Intersects(area.geometry, seg._c)
) TO '{OUT / 'segment.parquet'}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
print(c.sql(f"SELECT country, subtype, count(*) AS n FROM read_parquet('{OUT / 'segment.parquet'}') GROUP BY ALL ORDER BY country, n DESC"))

el('connector: 残った segment から参照されているものだけ')
c.sql(f"""COPY (
    SELECT * FROM read_parquet('{RAW / 'connector_bbox.parquet'}')
    WHERE id IN (SELECT DISTINCT unnest(connectors).connector_id FROM read_parquet('{OUT / 'segment.parquet'}'))
) TO '{OUT / 'connector.parquet'}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
el(f'  {count(OUT / "connector.parquet"):,} 件')

for p in sorted(list(RAW.glob('*.parquet')) + list(OUT.glob('*.parquet'))):
    print(f'  {p.relative_to(BASE)}  {p.stat().st_size / 1e9:.2f} GB')
el('完了')
