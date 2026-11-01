"""到達圏の一覧（scripts/13_reach_maps.py --cities pop100）に使う、人口の上位 100 都市を選ぶ

人口: Natural Earth の populated places（1:10m、v5.1.2、パブリックドメイン）の POP_MAX（都市圏の人口）。
    https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places.zip を
    data/raw/naturalearth_populated_places/ に展開しておく

POP_MAX の大きい順に 100 都市を取る。ただし次の都市は除いて、101 位以下で埋める（EXCLUDE）:
- 人口が都市圏ではなく行政区域の値らしいもの: 河池・アマラーヴァティー・漳州
- 別の都市の都市圏に含まれるもの: ハウラー（コルカタ）・新北（台北）・横浜（東京）

中心は Natural Earth の点（都心の代表点）。結果は scripts/cities_pop100.csv（順位・名前・人口・緯度経度）と、
除いた都市の scripts/cities_pop100_excluded.csv。

    uv run python scripts/15_city_list.py
"""

import csv
import re
import unicodedata
from pathlib import Path

import duckdb

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / 'data' / 'raw' / 'naturalearth_populated_places' / 'ne_10m_populated_places.shp'
OUT = BASE / 'scripts' / 'cities_pop100.csv'
N = 100
EXCLUDE = {  # NAMEASCII: (理由, 英語の理由)
    'Hechi': ('人口が行政区域（地級市）の値', 'administrative-area population'),
    'Amaravati': ('人口が行政区域の値（建設中の州都）', 'administrative-area population'),
    'Zhangzhou': ('人口が行政区域（地級市）の値', 'administrative-area population'),
    'Haora': ('コルカタの都市圏', 'part of the Kolkata metro'),
    'New Taipei': ('台北の都市圏', 'part of the Taipei metro'),
    'Yokohama': ('東京の都市圏', 'part of the Tokyo metro'),
}
OUT_EX = BASE / 'scripts' / 'cities_pop100_excluded.csv'   # 除いた都市（図の注記に使う）
FIX_EN = {'Shenyeng': 'Shenyang', 'Ōsaka': 'Osaka'}   # Natural Earth の綴りの誤り・英語で普通の綴り


def name_ja(s):
    """末尾の「市」「都」「特別市」「広域市」を落とす（東京都 → 東京、ホーチミン市 → ホーチミン）"""
    return re.sub(r'(特別市|広域市|市|都)$', '', s)


def fname(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


c = duckdb.connect()
c.sql('INSTALL spatial; LOAD spatial')
rows = c.sql(f"""
    SELECT NAME, NAMEASCII, NAME_JA, ADM0NAME, POP_MAX, LATITUDE, LONGITUDE
    FROM st_read('{SRC.as_posix()}') ORDER BY POP_MAX DESC LIMIT {N + 50}
""").fetchall()

out, skipped = [], []
for name, ascii_, ja, country, pop, lat, lon in rows:
    if ascii_ in EXCLUDE:
        skipped.append({'name_ja': name_ja(ja), 'name_en': ascii_, 'pop_max': pop,
                        'reason_ja': EXCLUDE[ascii_][0], 'reason_en': EXCLUDE[ascii_][1]})
        continue
    en = FIX_EN.get(name, re.sub(r'\s+', ' ', name))
    out.append({'rank': len(out) + 1, 'name_ja': name_ja(ja), 'name_en': en,
                'fname': fname(en), 'country': country,
                'pop_max': pop, 'lat': round(lat, 6), 'lon': round(lon, 6)})
    if len(out) == N:
        break

assert len({r['fname'] for r in out}) == N, 'ファイル名が重複'
assert len(skipped) == len(EXCLUDE), f'除く都市が上位に無い: {set(EXCLUDE) - {s["name_en"] for s in skipped}}'
with open(OUT, 'w', encoding='utf-8', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(out[0]))
    w.writeheader()
    w.writerows(out)
with open(OUT_EX, 'w', encoding='utf-8', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(skipped[0]))
    w.writeheader()
    w.writerows(skipped)
for s in skipped:
    print(f"除外: {s['name_en']}（{s['pop_max']:,}）{s['reason_ja']}")
print(f'{len(out)} 都市 → {OUT}（100 位 {out[-1]["name_en"]} {out[-1]["pop_max"]:,}）')
