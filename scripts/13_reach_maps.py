"""主要都市の到達圏を同じ縮尺の地図画像にする（PLAN.md 段階 7・issue #4）

2 段で動く。API を 2 つ（速度 posted / class）同時に立てると 32 GiB 使うので、問い合わせた結果をいったん保存する。

1. fetch: 起動中の API（/reachability）に都市ごとの到達圏を問い合わせ、network/<case>/reach/<速度>_<分>/ に保存する
2. draw: 保存した到達圏を描く。リンクの形は組み立て済みグラフの座標（メモリマップ）から引く
   - 速度を 1 つ: 都市ごとの画像と一覧 all.png
   - 速度を 2 つ以上（posted,legal,class）: 都市ごとに左から並べた compare.png と、数字の表 stats.md

速度（api/graph.py）:
- posted: 制限速度があればその値、無ければ class の既定値。制限速度の入り方は国でまちまち
- legal: 制限速度が無い区間を国（州）の法定速度で埋める（scripts/14_legal_speeds.py）
- class: 全リンクを class の既定値で走る。都市どうしを同じ条件で比べられる

比べる数字:
- 到達面積: 届いたリンクの点が入る 5 km 四方のマスの数 × 25 km²。道路の分類や密度に左右されにくい
- 道路の延長・届く直線距離（99% 点）

    ROUTE_CASE=world uv run uvicorn api.main:app --port 8010                     # 速度 posted
    uv run python scripts/13_reach_maps.py fetch --speed posted
    ROUTE_CASE=world ROUTE_SPEED=class uv run uvicorn api.main:app --port 8010  # 速度 class
    uv run python scripts/13_reach_maps.py fetch --speed class
    ROUTE_CASE=world ROUTE_SPEED=legal uv run uvicorn api.main:app --port 8010  # 速度 legal
    uv run python scripts/13_reach_maps.py fetch --speed legal
    uv run python scripts/13_reach_maps.py draw --speeds posted,legal,class --half-km 300 --ring-km 150
    → docs/images/reach_120_posted_legal_class/compare.png・stats.md
"""

import argparse
import csv
import json
import math
import urllib.request
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CITIES = {  # 名前: (緯度, 経度, ファイル名)
    '東京': (35.6812, 139.7671, 'tokyo'),
    'ニューヨーク': (40.7580, -73.9855, 'new_york'),
    'ロサンゼルス': (34.0522, -118.2437, 'los_angeles'),
    'ロンドン': (51.5074, -0.1278, 'london'),
    'パリ': (48.8566, 2.3522, 'paris'),
    '北京': (39.9042, 116.4074, 'beijing'),
    'デリー': (28.6139, 77.2090, 'delhi'),
    'サンパウロ': (-23.5505, -46.6333, 'sao_paulo'),
}
EXTRA_CITIES = {  # 頼まれて描いた都市。--cities で選ぶ（README の all.png・compare.png には入れない）
    'エドモントン': (53.5461, -113.4938, 'edmonton'),
}
WORLD50 = {  # --cities world50: 大陸の偏りが出ないように選んだ 50 都市（中心は都心の代表点）
    # アジア
    '東京': (35.6812, 139.7671, 'tokyo'),
    '大阪': (34.7025, 135.4959, 'osaka'),
    'ソウル': (37.5665, 126.9780, 'seoul'),
    '北京': (39.9042, 116.4074, 'beijing'),
    '上海': (31.2304, 121.4737, 'shanghai'),
    '香港': (22.3027, 114.1772, 'hong_kong'),
    '台北': (25.0478, 121.5170, 'taipei'),
    'バンコク': (13.7563, 100.5018, 'bangkok'),
    'ジャカルタ': (-6.2088, 106.8456, 'jakarta'),
    'マニラ': (14.5995, 120.9842, 'manila'),
    'シンガポール': (1.2903, 103.8519, 'singapore'),
    'ホーチミン': (10.7769, 106.7009, 'ho_chi_minh'),
    'デリー': (28.6139, 77.2090, 'delhi'),
    'ムンバイ': (19.0760, 72.8777, 'mumbai'),
    'ダッカ': (23.8103, 90.4125, 'dhaka'),
    'カラチ': (24.8607, 67.0011, 'karachi'),
    'テヘラン': (35.6892, 51.3890, 'tehran'),
    'ドバイ': (25.2048, 55.2708, 'dubai'),
    'イスタンブール': (41.0082, 28.9784, 'istanbul'),
    # ヨーロッパ
    'ロンドン': (51.5074, -0.1278, 'london'),
    'パリ': (48.8566, 2.3522, 'paris'),
    'ベルリン': (52.5200, 13.4050, 'berlin'),
    'マドリード': (40.4168, -3.7038, 'madrid'),
    'ローマ': (41.9028, 12.4964, 'rome'),
    'モスクワ': (55.7558, 37.6173, 'moscow'),
    'ワルシャワ': (52.2297, 21.0122, 'warsaw'),
    'ストックホルム': (59.3293, 18.0686, 'stockholm'),
    'アムステルダム': (52.3676, 4.9041, 'amsterdam'),
    'ウィーン': (48.2082, 16.3738, 'vienna'),
    # アフリカ
    'カイロ': (30.0444, 31.2357, 'cairo'),
    'ラゴス': (6.5244, 3.3792, 'lagos'),
    'ナイロビ': (-1.2921, 36.8219, 'nairobi'),
    'ヨハネスブルグ': (-26.2041, 28.0473, 'johannesburg'),
    'キンシャサ': (-4.4419, 15.2663, 'kinshasa'),
    'カサブランカ': (33.5731, -7.5898, 'casablanca'),
    # 北アメリカ
    'ニューヨーク': (40.7580, -73.9855, 'new_york'),
    'ロサンゼルス': (34.0522, -118.2437, 'los_angeles'),
    'シカゴ': (41.8781, -87.6298, 'chicago'),
    'ヒューストン': (29.7604, -95.3698, 'houston'),
    'アトランタ': (33.7490, -84.3880, 'atlanta'),
    'トロント': (43.6532, -79.3832, 'toronto'),
    'エドモントン': (53.5461, -113.4938, 'edmonton'),
    'メキシコシティ': (19.4326, -99.1332, 'mexico_city'),
    # 南アメリカ
    'サンパウロ': (-23.5505, -46.6333, 'sao_paulo'),
    'ブエノスアイレス': (-34.6037, -58.3816, 'buenos_aires'),
    'リマ': (-12.0464, -77.0428, 'lima'),
    'ボゴタ': (4.7110, -74.0721, 'bogota'),
    'サンティアゴ': (-33.4489, -70.6693, 'santiago'),
    # オセアニア
    'シドニー': (-33.8688, 151.2093, 'sydney'),
    'オークランド': (-36.8485, 174.7633, 'auckland'),
}
SPEED_LABEL = {'posted': '制限速度を使う', 'legal': '無い区間は法定速度', 'class': '道路の種類ごとの同じ速度'}
SPEED_LABEL_EN = {'posted': 'posted speed limits', 'legal': 'legal default speeds where unposted',
                  'class': 'same speed per road class'}
NAME_EN = {  # 英語名（--lang en）。ここに無い都市はファイル名から作る（new_york → New York）
    'sao_paulo': 'São Paulo', 'bogota': 'Bogotá', 'ho_chi_minh': 'Ho Chi Minh City',
}
CELL_KM = 5

ap = argparse.ArgumentParser()
ap.add_argument('step', choices=['fetch', 'draw'])
ap.add_argument('--case', default='world')
ap.add_argument('--api', default='http://127.0.0.1:8010')
ap.add_argument('--limit', type=int, default=120, help='到達圏の時間（分）')
ap.add_argument('--no-expressway', action='store_true')
ap.add_argument('--speed', default='posted', help='fetch: API の速度（/health で確かめる）')
ap.add_argument('--speeds', default='posted', help='draw: 描く速度。カンマ区切りで 2 つなら左右に並べる')
ap.add_argument('--half-km', type=float, default=300, help='地図の範囲（中心から上下左右の km）')
ap.add_argument('--major', action='store_true', help='motorway〜primary だけ描く（画面のズーム 7〜8 と同じ）')
ap.add_argument('--ring-km', type=float, help='中心からこの半径の円を描く（届く距離の目安）')
ap.add_argument('--cities', help='都市のファイル名をカンマ区切りで（例: edmonton）か world50。省略すると主要 8 都市')
ap.add_argument('--sort-area', action='store_true', help='draw: 一覧を到達面積の大きい順に並べる')
ap.add_argument('--no-each', action='store_true', help='draw: 都市ごとの画像を作らず一覧だけ作る')
ap.add_argument('--cols', type=int, default=4, help='draw: 一覧の列の数（速度 1 つのとき）')
ap.add_argument('--top', type=int, help='--cities pop100 のうち人口の上位この数だけ使う（例: 50）')
ap.add_argument('--dpi', type=int, default=150, help='draw: 一覧の画像の解像度')
ap.add_argument('--lang', choices=['ja', 'en'], default='ja', help='draw: 図の文字の言語。en は画像の名前に _en を付ける')
ap.add_argument('--skip-existing', action='store_true', help='fetch: 保存済みの都市は問い合わせない')
a = ap.parse_args()

group = ''   # 中心の点の出どころが違う都市の組は、到達圏を別のフォルダに保存する
if a.cities == 'pop100':
    # 人口の上位 100 都市（scripts/15_city_list.py が作る。中心は Natural Earth の点）
    with open(BASE / 'scripts' / 'cities_pop100.csv', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    rows = rows[:a.top] if a.top else rows
    CITIES = {r['name_ja']: (float(r['lat']), float(r['lon']), r['fname']) for r in rows}
    NAME_EN.update({r['fname']: r['name_en'] for r in rows})
    with open(BASE / 'scripts' / 'cities_pop100_excluded.csv', encoding='utf-8') as f:   # 上位に入るのに除いた都市
        EXCLUDED = [r for r in csv.DictReader(f) if int(r['pop_max']) >= int(rows[-1]['pop_max'])]
    picked = f'_pop{len(rows)}'
    group = '_pop100'
elif a.cities == 'world50':
    CITIES = WORLD50
    picked = '_world50'
elif a.cities:
    by_fname = {v[2]: (k, v) for k, v in {**CITIES, **EXTRA_CITIES, **WORLD50}.items()}
    CITIES = dict(by_fname[f] for f in a.cities.split(','))
    picked = '_' + '_'.join(v[2] for v in CITIES.values())   # 一覧の画像の名前に付ける
else:
    picked = ''

tag = f'{a.limit}{"_noexp" if a.no_expressway else ""}'
REACH = BASE / 'network' / a.case / 'reach'


def proj(lon, lat, lon0, lat0):
    """中心からの km（正距円筒を中心の緯度で縮めたもの。±300 km なら十分）"""
    return (lon - lon0) * math.cos(math.radians(lat0)) * 111.32, (lat - lat0) * 110.57


# ------------------------------------------------------------------ 1. fetch
if a.step == 'fetch':
    with urllib.request.urlopen(f'{a.api}/health', timeout=30) as r:
        h = json.load(r)
    assert h.get('case') == a.case and h.get('speed', 'posted') == a.speed, \
        f"API は case={h.get('case')} speed={h.get('speed')}（--case {a.case} --speed {a.speed} と違う）"
    d = REACH / f'{a.speed}_{tag}{group}'
    d.mkdir(parents=True, exist_ok=True)
    for name, (lat, lon, fname) in CITIES.items():
        if a.skip_existing and (d / f'{fname}.npz').exists():
            continue
        # road_class=all: 間引かずに全リンクを受け取る（auto だと 100 万本を超えたとき都市ごとに絞り方が変わる）
        q = (f'{a.api}/reachability?lat={lat}&lon={lon}&limit_min={a.limit}&road_class=all'
             f'&use_expressway={str(not a.no_expressway).lower()}')
        with urllib.request.urlopen(q, timeout=600) as r:
            res = json.load(r)
        np.savez(d / f'{fname}.npz', link_ids=np.asarray(res['link_ids'], dtype=np.int64),
                 costs=np.asarray(res['costs'], dtype=np.float32), snap_m=res['snap']['dist_m'])
        print(f"{name}: {len(res['link_ids']):,} リンク・{res['elapsed_ms']:,.0f} ms", flush=True)
    print(f'保存: {d}')
    raise SystemExit

# ------------------------------------------------------------------ 2. draw
speeds = a.speeds.split(',')
g = BASE / 'network' / a.case / 'graph'
coords = np.load(g / 'coords.npy', mmap_mode='r')
offsets = np.load(g / 'coord_offsets.npy', mmap_mode='r')
link_dist = np.load(g / 'link_dist.npy', mmap_mode='r')
major = np.load(g / 'mask_trunk.npy') if a.major else None   # motorway・trunk・primary
print('リンクの始点を読む', flush=True)
first = np.asarray(coords[np.asarray(offsets[:-1])])   # リンクごとの始点（背景の道路を範囲で選ぶため）


def segments(ids, lon0, lat0):
    ids = np.asarray(ids, dtype=np.int64)
    lo, hi = np.asarray(offsets[ids]), np.asarray(offsets[ids + 1])
    n = hi - lo
    idx = np.repeat(lo - np.cumsum(np.concatenate([[0], n[:-1]])), n) + np.arange(n.sum())
    xy = np.asarray(coords[idx], dtype=np.float64)
    x, y = proj(xy[:, 0], xy[:, 1], lon0, lat0)
    return np.split(np.column_stack([x, y]), np.cumsum(n)[:-1]), x, y


bands = a.limit // 10 if a.limit <= 240 else 12
cmap = plt.get_cmap('plasma_r', bands)
bg, res = {}, {}
for name, (lat, lon, fname) in CITIES.items():
    dlat = a.half_km / 110.57
    dlon = a.half_km / (111.32 * math.cos(math.radians(lat)))
    m = (np.abs(first[:, 1] - lat) <= dlat * 1.05) & (np.abs(first[:, 0] - lon) <= dlon * 1.05)
    bg[name] = segments(np.flatnonzero(m & major) if a.major else np.flatnonzero(m), lon, lat)[0]
    for sp in speeds:
        z = np.load(REACH / f'{sp}_{tag}{group}' / f'{fname}.npz')
        ids, costs = z['link_ids'], z['costs']
        km = float(np.asarray(link_dist[ids]).sum() / 1000)     # 延長と面積は全 class で数える（--major は描くだけ）
        _, x, y = segments(ids, lon, lat)
        cells = np.unique(np.floor(x / CELL_KM).astype(np.int64) * 100_000 + np.floor(y / CELL_KM).astype(np.int64))
        st = {'km': km, 'area': len(cells) * CELL_KM ** 2,
              'reach_km': float(np.percentile(np.hypot(x, y), 99)) if len(ids) else 0.0}
        if a.major:
            keep = major[ids]
            ids, costs = ids[keep], costs[keep]
        order = np.argsort(-costs)                 # 近い方を上に描く
        st['seg'] = segments(ids[order], lon, lat)[0]
        st['band'] = np.minimum((costs[order] / (a.limit / bands)).astype(int), bands - 1)
        res[name, sp] = st
        print(f"{name} [{sp}]: 面積 {st['area']:,} km²・道路 {km:,.0f} km・直線 {st['reach_km']:.0f} km", flush=True)


EN = a.lang == 'en'


def T(ja, en):
    return en if EN else ja


def disp(name):
    """図に出す都市名"""
    f = CITIES[name][2]
    return NAME_EN.get(f, f.replace('_', ' ').title()) if EN else name


def speed_label(sp):
    return SPEED_LABEL_EN[sp] if EN else SPEED_LABEL[sp]


def draw(ax, name, sp, title, small=False):
    r = res[name, sp]
    ax.set_facecolor('#f4f3ef')
    ax.add_collection(LineCollection(bg[name], colors='#c9c6bd', linewidths=0.2 if small else 0.35))
    ax.add_collection(LineCollection(r['seg'], colors=cmap(r['band']), linewidths=0.45 if small else 0.8))
    ax.plot(0, 0, marker='o', ms=5 if small else 7, mfc='white', mec='black', mew=1.2, zorder=5)
    if a.ring_km:
        t = np.linspace(0, 2 * np.pi, 361)
        ax.plot(a.ring_km * np.cos(t), a.ring_km * np.sin(t), color='#1a7f5a', lw=1.2 if small else 1.6, ls='--', zorder=4)
        ax.text(a.ring_km * 0.71, a.ring_km * 0.71, f' {a.ring_km:.0f} km', color='#1a7f5a',
                fontsize=8 if small else 10, va='bottom', zorder=4)
    h = a.half_km
    ax.set_xlim(-h, h)
    ax.set_ylim(-h, h)
    ax.set_aspect('equal')
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"{title}\n" + T(f"面積 {r['area']:,} km²・直線 {r['reach_km']:.0f} km",
                                 f"area {r['area']:,} km² · reach {r['reach_km']:.0f} km"), fontsize=10 if small else 13)
    ax.plot([-h * 0.9, -h * 0.9 + 100], [-h * 0.9] * 2, color='black', lw=2)       # 縮尺 100 km
    ax.text(-h * 0.9 + 50, -h * 0.86, '100 km', ha='center', fontsize=7 if small else 9)


def colorbar(fig, axes):
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, a.limit))
    cb = fig.colorbar(sm, ax=axes, shrink=0.5, pad=0.01, ticks=range(0, a.limit + 1, a.limit // bands * (2 if bands > 8 else 1)))
    cb.set_label(T('所要時間（分）', 'Travel time (min)'))


mode = '高速道路あり' if not a.no_expressway else '高速道路なし'
shown = 'motorway〜primary を表示' if a.major else '幹線（motorway〜tertiary）を表示'
note = (f'{shown}・車・{mode}・{a.limit} 分・中心から ±{a.half_km:.0f} km・面積は届いた道路が通る {CELL_KM} km 四方のマスの合計\n'
        '© Overture Maps Foundation（2026-10-21.0）, © OpenStreetMap contributors')
if a.cities == 'pop100':
    ex = '、'.join(f"{r['name_ja']}（{r['reason_ja']}）" for r in EXCLUDED)
    note = note.replace('\n', f'\n都市: Natural Earth（populated places、POP_MAX）の人口の上位 {len(CITIES)}'
                        + (f'。除いた都市: {ex}' if ex else '') + '\n', 1)
if EN:
    note = (('Major roads (motorway–primary) shown' if a.major else 'Main roads (motorway–tertiary) shown')
            + f' · car · {"with" if not a.no_expressway else "without"} motorways · {a.limit} min · ±{a.half_km:.0f} km from the center'
            + f' · area = {CELL_KM} km grid cells crossed by reached roads\n'
            '© Overture Maps Foundation (2026-10-21.0), © OpenStreetMap contributors')
    if a.cities == 'pop100':
        ex = ', '.join(f"{r['name_en']} ({r['reason_en']})" for r in EXCLUDED)
        note = note.replace('\n', f'\nCities: top {len(CITIES)} by population (Natural Earth populated places, POP_MAX)'
                            + (f', excluding {ex}' if ex else '') + '\n', 1)


def footnote(fig, size):
    # supxlabel は constrained layout が下に余白を取るので、地図の枠と重ならない
    fig.supxlabel(note, x=0.01, ha='left', fontsize=size, color='#555', linespacing=1.4)
suffix = ('_' + '_'.join(speeds) if len(speeds) > 1 or speeds[0] != 'posted' else '') + ('_major' if a.major else '')
out = BASE / 'docs' / 'images' / f'reach_{tag}{suffix}'
lang = '_en' if EN else ''   # 英語版の画像の名前に付ける
out.mkdir(parents=True, exist_ok=True)
plt.rcParams['font.family'] = ['Yu Gothic', 'Meiryo', 'sans-serif']

if len(speeds) == 1:
    sp = speeds[0]
    for name, (_, _, fname) in ({} if a.no_each else CITIES).items():
        fig, ax = plt.subplots(figsize=(8, 7.4), dpi=150, layout='constrained')
        draw(ax, name, sp, disp(name))
        colorbar(fig, [ax])
        footnote(fig, 7)
        fig.savefig(out / f'{fname}{lang}.png', bbox_inches='tight')
        plt.close(fig)
    if len(CITIES) == 1:   # 1 都市なら一覧は都市の画像と同じなので作らない
        print(f'書き出し: {out}')
        raise SystemExit
    names = sorted(CITIES, key=lambda c: -res[c, sp]['area']) if a.sort_area else list(CITIES)
    cols = min(a.cols, len(CITIES))
    rows = math.ceil(len(CITIES) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 4.6 * rows), dpi=a.dpi, layout='constrained', squeeze=False)
    for k, (ax, name) in enumerate(zip(axes.flat, names)):
        draw(ax, name, sp, f'{k + 1}. {disp(name)}' if a.sort_area else disp(name), small=True)
    for ax in axes.flat[len(names):]:
        ax.set_axis_off()
    colorbar(fig, axes.ravel().tolist())
    order = T('・到達面積の大きい順', ', ranked by area') if a.sort_area else ''
    fig.suptitle(T(f'到達圏 {a.limit} 分の比較（{len(CITIES)} 都市・同じ縮尺・{SPEED_LABEL[sp]}{order}）',
                   f'How far can you drive in {a.limit} minutes? {len(CITIES)} cities at the same scale ({speed_label(sp)}{order})'),
                 fontsize=15 if cols <= 4 else 30)
    footnote(fig, 8)
    fig.savefig(out / f'all{picked}{lang}.png', bbox_inches='tight')
else:
    # 都市ごとに速度を左から並べる。1 行に 2 都市
    n = len(speeds)
    cols = n * min(2, len(CITIES))
    rows = math.ceil(len(CITIES) / 2)
    fig, axes = plt.subplots(rows, cols, figsize=(3.9 * cols, 4.5 * rows), dpi=a.dpi, layout='constrained', squeeze=False)
    for k, name in enumerate(CITIES):
        for j, sp in enumerate(speeds):
            draw(axes.flat[n * k + j], name, sp, T(f'{name}（{SPEED_LABEL[sp]}）', f'{disp(name)} ({speed_label(sp)})'), small=True)
    colorbar(fig, axes.ravel().tolist())
    fig.suptitle(T(f'到達圏 {a.limit} 分：' + '｜'.join(SPEED_LABEL[sp] for sp in speeds) + '（左から）',
                   f'{a.limit}-minute drive: ' + ' | '.join(speed_label(sp) for sp in speeds) + ' (left to right)'), fontsize=15)
    footnote(fig, 8)
    fig.savefig(out / f'compare{picked}{lang}.png', bbox_inches='tight')

    base = speeds[0]
    lines = [f'# 到達圏 {a.limit} 分の数字（{mode}）', '',
             '速度: ' + '、'.join(f'{sp} = {SPEED_LABEL[sp]}' for sp in speeds) + f'。面積は届いた道路が通る {CELL_KM} km 四方のマスの合計。'
             f'比は {base} に対する比。延長は全 class。', '',
             '| 都市 | ' + ' | '.join(f'面積 km² {sp}' for sp in speeds) + ' | ' + ' | '.join(f'比 {sp}' for sp in speeds[1:])
             + ' | ' + ' | '.join(f'直線 km {sp}' for sp in speeds) + ' |',
             '|---' * (1 + 2 * n + n - 1) + '|']
    for name in sorted(CITIES, key=lambda c: -res[c, speeds[-1]]['area']):
        r = [res[name, sp] for sp in speeds]
        lines.append(f'| {name} | ' + ' | '.join(f"{x['area']:,}" for x in r) + ' | '
                     + ' | '.join(f"{x['area'] / r[0]['area']:.2f}" for x in r[1:]) + ' | '
                     + ' | '.join(f"{x['reach_km']:.0f}" for x in r) + ' |')
    (out / f'stats{picked}.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
print(f'書き出し: {out}')
