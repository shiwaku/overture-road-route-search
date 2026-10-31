"""主要都市の到達圏を同じ縮尺の地図画像にする（PLAN.md 段階 7）

起動中の API（/reachability）に到達圏を問い合わせ、リンクの形は組み立て済みグラフの座標（メモリマップ）から引く。
API とは別に RoadGraph を読み込まないので、メモリは座標の分しか使わない。

    ROUTE_CASE=world uv run uvicorn api.main:app --port 8010   # 先に起動しておく
    uv run python scripts/13_reach_maps.py --limit 120
    → docs/images/reach_<limit>/<都市>.png と一覧 all.png
"""

import argparse
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

ap = argparse.ArgumentParser()
ap.add_argument('--case', default='world')
ap.add_argument('--api', default='http://127.0.0.1:8010')
ap.add_argument('--limit', type=int, default=120, help='到達圏の時間（分）')
ap.add_argument('--half-km', type=float, default=250, help='地図の範囲（中心から上下左右の km）')
ap.add_argument('--no-expressway', action='store_true')
ap.add_argument('--major', action='store_true', help='motorway〜primary だけ描く（画面のズーム 7〜8 と同じ）')
ap.add_argument('--ring-km', type=float, help='中心からこの半径の円を描く（届く距離の目安）')
a = ap.parse_args()

out = BASE / 'docs' / 'images' / f'reach_{a.limit}{"_noexp" if a.no_expressway else ""}{"_major" if a.major else ""}'
out.mkdir(parents=True, exist_ok=True)
plt.rcParams['font.family'] = ['Yu Gothic', 'Meiryo', 'sans-serif']

g = BASE / 'network' / a.case / 'graph'
coords = np.load(g / 'coords.npy', mmap_mode='r')
offsets = np.load(g / 'coord_offsets.npy', mmap_mode='r')
link_dist = np.load(g / 'link_dist.npy', mmap_mode='r')
major = np.load(g / 'mask_trunk.npy') if a.major else None   # motorway・trunk・primary
print('リンクの始点を読む', flush=True)
first = np.asarray(coords[np.asarray(offsets[:-1])])   # リンクごとの始点（背景の道路を範囲で選ぶため）


def proj(lon, lat, lon0, lat0):
    """中心からの km（正距円筒を中心の緯度で縮めたもの。±250 km なら十分）"""
    return (lon - lon0) * math.cos(math.radians(lat0)) * 111.32, (lat - lat0) * 110.57


def segments(ids, lon0, lat0):
    ids = np.asarray(ids, dtype=np.int64)
    lo, hi = np.asarray(offsets[ids]), np.asarray(offsets[ids + 1])
    n = hi - lo
    idx = np.repeat(lo - np.cumsum(np.concatenate([[0], n[:-1]])), n) + np.arange(n.sum())
    xy = np.asarray(coords[idx], dtype=np.float64)
    x, y = proj(xy[:, 0], xy[:, 1], lon0, lat0)
    return np.split(np.column_stack([x, y]), np.cumsum(n)[:-1])


def reach(lat, lon):
    q = f'{a.api}/reachability?lat={lat}&lon={lon}&limit_min={a.limit}&use_expressway={str(not a.no_expressway).lower()}'
    with urllib.request.urlopen(q, timeout=600) as r:
        return json.load(r)


bands = a.limit // 10 if a.limit <= 240 else 12
cmap = plt.get_cmap('plasma_r', bands)
results = {}
for name, (lat, lon, fname) in CITIES.items():
    r = reach(lat, lon)
    ids, costs = np.asarray(r['link_ids']), np.asarray(r['costs'])
    if a.major:
        keep = major[ids]
        ids, costs = ids[keep], costs[keep]
    dlat = a.half_km / 110.57
    dlon = a.half_km / (111.32 * math.cos(math.radians(lat)))
    m = (np.abs(first[:, 1] - lat) <= dlat * 1.05) & (np.abs(first[:, 0] - lon) <= dlon * 1.05)
    bg = np.flatnonzero(m & major) if a.major else np.flatnonzero(m)
    x, y = proj(first[ids, 0], first[ids, 1], lon, lat)
    st = {
        'links': len(ids), 'applied': r['road_class_applied'], 'counts': r['counts'],
        'km': float(np.asarray(link_dist[ids]).sum() / 1000),
        'reach_km': float(np.percentile(np.hypot(x, y), 99)) if len(ids) else 0.0,
        'elapsed_ms': r['elapsed_ms'], 'snap_m': r['snap']['dist_m'],
    }
    results[name] = st
    print(f"{name}: リンク {st['links']:,}（{st['applied']}）・道路 {st['km']:,.0f} km・"
          f"直線で {st['reach_km']:.0f} km（99%）・背景 {len(bg):,} 本・API {st['elapsed_ms']:,.0f} ms", flush=True)
    results[name]['bg'] = segments(bg, lon, lat)
    order = np.argsort(-costs)                 # 近い方を上に描く
    results[name]['seg'] = segments(ids[order], lon, lat)
    results[name]['band'] = np.minimum((costs[order] / (a.limit / bands)).astype(int), bands - 1)


def draw(ax, name, small=False):
    r = results[name]
    ax.set_facecolor('#f4f3ef')
    ax.add_collection(LineCollection(r['bg'], colors='#c9c6bd', linewidths=0.25 if small else 0.35))
    ax.add_collection(LineCollection(r['seg'], colors=cmap(r['band']), linewidths=0.5 if small else 0.8))
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
    ax.set_title(f"{name}\n道路 {r['km']:,.0f} km・直線 {r['reach_km']:.0f} km", fontsize=10 if small else 13)
    # 縮尺 100 km
    ax.plot([-h * 0.9, -h * 0.9 + 100], [-h * 0.9] * 2, color='black', lw=2)
    ax.text(-h * 0.9 + 50, -h * 0.86, '100 km', ha='center', fontsize=7 if small else 9)


def colorbar(fig, axes):
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, a.limit))
    cb = fig.colorbar(sm, ax=axes, shrink=0.6, pad=0.02, ticks=range(0, a.limit + 1, a.limit // bands * (2 if bands > 8 else 1)))
    cb.set_label('所要時間（分）')


mode = '高速道路あり' if not a.no_expressway else '高速道路なし'
shown = 'motorway〜primary を表示（探索は tertiary まで）' if a.major else '幹線（motorway〜tertiary）'
note = f'Overture Maps 2026-10-21.0・{shown}・車・{mode}・{a.limit} 分・中心から ±{a.half_km:.0f} km'
for name, (_, _, fname) in CITIES.items():
    fig, ax = plt.subplots(figsize=(8, 7.4), dpi=150, layout='constrained')
    draw(ax, name)
    colorbar(fig, [ax])
    fig.text(0.01, 0.01, note, fontsize=7, color='#555')
    fig.savefig(out / f'{fname}.png', bbox_inches='tight')
    plt.close(fig)

cols = 4
rows = math.ceil(len(CITIES) / cols)
fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 4.6 * rows), dpi=150, layout='constrained')
for ax, name in zip(axes.flat, CITIES):
    draw(ax, name, small=True)
for ax in list(axes.flat)[len(CITIES):]:
    ax.axis('off')
colorbar(fig, axes.ravel().tolist())
fig.suptitle(f'到達圏 {a.limit} 分の比較（同じ縮尺）', fontsize=15)
fig.text(0.01, 0.005, note, fontsize=8, color='#555')
fig.savefig(out / 'all.png', bbox_inches='tight')
print(f'書き出し: {out}')
