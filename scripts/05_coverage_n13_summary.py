"""N13 との重ね合わせ（04_coverage_n13.py の結果）を、道路種別・幅員・都道府県・2 次メッシュで集計する（PLAN.md 段階 2）

「Overture にある」= N13 の点から MATCH_M 以内に Overture の道（class は問わない）がある。
2 次メッシュは中心点が入る都道府県に割り当てる（海に出る場合はいちばん近い都道府県）。

    uv run python scripts/05_coverage_n13_summary.py
出力: output/coverage/n13_by_type.csv, n13_by_pref.csv, n13_by_mesh2.parquet（QGIS 用・2 次メッシュの四角形付き）
"""

from pathlib import Path

import duckdb
import geopandas  # noqa: F401  GeoParquet を書くため（無ければ WKB の列だけ書く）
import numpy as np
import pandas as pd
import shapely

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
RAW = BASE / 'data' / 'raw' / RELEASE
OUT = BASE / 'output' / 'coverage'
MATCH_M = 15
ROAD_TYPE = {'1': '国道', '2': '都道府県道', '3': '市区町村道', '4': '高速', '5': 'その他', '6': '不明'}
WIDTH = {'1': '3m未満', '2': '3〜5.5m', '3': '5.5m以上', '4': '5.5m以上', '5': '5.5m以上', '6': '不明'}

d = pd.read_parquet(OUT / 'n13_match.parquet')
d['ok'] = d.dist_bin.isin([b for b in ['<=5', '<=10', '<=15', '<=20', '<=30', '<=50'] if int(b[2:]) <= MATCH_M])
d['km'] = d.length_m / 1000
d['ok_km'] = d.km.where(d.ok, 0)


def summarize(keys):
    g = d.groupby(keys, as_index=False)[['km', 'ok_km']].sum()
    g['rate'] = (g.ok_km / g.km).round(3)
    return g.round({'km': 1, 'ok_km': 1})


# 道路種別 × 幅員
d['road_type_name'] = d.road_type.map(ROAD_TYPE)
d['width_name'] = d.width.map(WIDTH)
by_type = summarize(['road_type', 'road_type_name', 'width_name'])
by_type.to_csv(OUT / 'n13_by_type.csv', index=False, encoding='utf-8-sig')
print(f'N13 の道の点から {MATCH_M} m 以内に Overture の道がある延長の割合')
print(by_type.to_string(index=False))

# 2 次メッシュ → 都道府県
m2 = pd.DataFrame({'mesh2': d.mesh2.unique()})
s = m2.mesh2.astype(str)
lat_s = (s.str[:2].astype(int) + s.str[4].astype(int) / 8) / 1.5
lon_w = s.str[2:4].astype(int) + 100 + s.str[5].astype(int) / 8
m2['geometry'] = shapely.box(lon_w, lat_s, lon_w + 1 / 8, lat_s + 1 / 12)
c = duckdb.connect()
c.sql("LOAD spatial")
reg = c.sql(f"""SELECT region, ST_AsWKB(geometry) AS wkb FROM read_parquet('{RAW / 'divisions_region.parquet'}')
    WHERE class = 'land'""").df()
reg_geom = shapely.from_wkb(reg.wkb.map(bytes))
tree = shapely.STRtree(reg_geom)
center = shapely.centroid(m2.geometry.to_numpy())
ip, ir = tree.query_nearest(center, all_matches=False)
m2.loc[ip, 'pref'] = reg.region.to_numpy()[ir]

d = d.merge(m2[['mesh2', 'pref']], on='mesh2')
names = pd.read_csv(OUT / 'stats_by_pref.csv')[['pref', 'pref_name']].drop_duplicates()
d['narrow'] = np.where(d.width == '1', 'w3m未満', 'w3m以上')
by_pref = summarize(['pref', 'narrow']).pivot_table(index='pref', columns='narrow', values=['km', 'rate'])
by_pref.columns = [f'{a}_{b}' for a, b in by_pref.columns]
allp = summarize(['pref']).set_index('pref')[['km', 'rate']].add_suffix('_all')
by_pref = names.set_index('pref').join(allp).join(by_pref).reset_index()
stats = pd.read_csv(OUT / 'stats_by_pref.csv').query("group == 'total'")[['pref', 'ratio']].rename(columns={'ratio': 'stats_ratio'})
by_pref = by_pref.merge(stats, on='pref')
by_pref.to_csv(OUT / 'n13_by_pref.csv', index=False, encoding='utf-8-sig')
print('\n都道府県別（rate = N13 のうち Overture にある割合、stats_ratio = 車道延長の統計比）')
print(by_pref.to_string(index=False))

# 2 次メッシュ（地図用）
g = summarize(['mesh2'])
nar = d[d.width == '1'].groupby('mesh2')[['km', 'ok_km']].sum()
g = g.merge((nar.ok_km / nar.km).round(3).rename('rate_narrow').reset_index(), on='mesh2', how='left')
g = m2.merge(g, on='mesh2')
import geopandas as gpd
gpd.GeoDataFrame(g, geometry='geometry', crs='EPSG:4326').to_parquet(OUT / 'n13_by_mesh2.parquet')
print(f'\n2 次メッシュ {len(g):,} 個 → {(OUT / "n13_by_mesh2.parquet").relative_to(BASE)}')
