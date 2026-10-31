"""探索グラフの読み込み・到達圏・経路の速さとメモリを測る（PLAN.md 段階 7）

API を通さず RoadGraph を直接呼ぶ。経路は大陸をまたぐ長いものも入れる。

    uv run python scripts/12_bench.py --case world
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.graph import RoadGraph  # noqa: E402

PLACES = {
    '東京': (35.6812, 139.7671), '大阪': (34.7025, 135.4959), '札幌': (43.0687, 141.3508),
    'パリ': (48.8566, 2.3522), 'ベルリン': (52.5200, 13.4050), 'マドリード': (40.4168, -3.7038),
    'モスクワ': (55.7558, 37.6173), 'リスボン': (38.7223, -9.1393), '北京': (39.9042, 116.4074),
    'シンガポール': (1.2903, 103.8520), 'ニューヨーク': (40.7128, -74.0060), 'ロサンゼルス': (34.0522, -118.2437),
    'ケープタウン': (-33.9249, 18.4241), 'カイロ': (30.0444, 31.2357), 'ロンドン': (51.5074, -0.1278),
}
ROUTES = [('東京', '大阪'), ('東京', '札幌'), ('パリ', 'ベルリン'), ('マドリード', 'モスクワ'),
          ('ニューヨーク', 'ロサンゼルス'), ('リスボン', '北京'), ('リスボン', 'シンガポール'),
          ('ケープタウン', 'カイロ'), ('ロンドン', 'パリ')]
REACH = [('東京', 30), ('東京', 120), ('パリ', 60), ('ニューヨーク', 60), ('ニューヨーク', 240)]


def mem_gib():
    """(いまの物理メモリ, これまでの最大) GiB。Windows だけ"""
    if sys.platform != 'win32':
        return float('nan'), float('nan')
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    k32 = ctypes.WinDLL('kernel32')
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi')
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    m = PMC(cb=ctypes.sizeof(PMC))
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(m), m.cb)
    return m.WorkingSetSize / 2**30, m.PeakWorkingSetSize / 2**30


ap = argparse.ArgumentParser()
ap.add_argument('--case', default='world')
ap.add_argument('--speed', default='posted', help='posted・class（api/graph.py）')
a = ap.parse_args()

g = RoadGraph(a.case, speed=a.speed)
cur, peak = mem_gib()
print(f'読み込み: {g.load_seconds:.1f} 秒（{g.source}）・メモリ {cur:.1f} GiB（最大 {peak:.1f}）', flush=True)
h = g.health()
print(f"  links {h['links']:,} / nodes {h['nodes']:,} / スナップ先 {h['snap_nodes']:,} / 弱連結成分 {h['components']:,}")
sizes = np.sort(np.bincount(g.weak))[::-1]
print(f'  大きい道路網（弱連結）: ' + ' / '.join(f'{s:,}' for s in sizes[:8]), flush=True)
for name, (lat, lon) in PLACES.items():
    idx, _, _, sm = g.snap(lat, lon)
    print(f'  {name}: スナップ {sm:,.0f} m・{g.component_name(idx)}')

print('\n到達圏（高速道路あり）', flush=True)
for name, lim in REACH:
    r = g.reachability(*PLACES[name], lim)
    print(f"  {name} {lim:4d} 分: リンク {len(r['link_ids']):>9,}（{r['road_class_applied']}）"
          f"・ノード {r['nodes_reached']:>10,}・Dijkstra {r['dijkstra_ms']:7,.0f} ms・全体 {r['elapsed_ms']:7,.0f} ms", flush=True)

print('\n経路（高速道路あり）', flush=True)
for o, d in ROUTES:
    r = g.route(*PLACES[o], *PLACES[d])
    if r['unreachable_reason']:
        print(f"  {o}→{d}: {r['unreachable_reason']}（{r['elapsed_ms']:,.0f} ms）", flush=True)
        continue
    s = r['summary']
    print(f"  {o}→{d}: {s['time_min'] / 60:6.1f} 時間・{s['dist_m'] / 1000:7,.0f} km・リンク {s['link_count']:,}"
          f"・探索ノード {s['nodes_searched']:,}・{s['tries']} 回・{r['elapsed_ms'] / 1000:6.1f} 秒", flush=True)

cur, peak = mem_gib()
print(f'\nメモリ: いま {cur:.1f} GiB・最大 {peak:.1f} GiB')
