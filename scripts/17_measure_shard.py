"""大陸ごとに分けた探索グラフ（scripts/16_split_world.py）を読み込み、メモリ・読み込み時間・探索の速さを測る（issue #9）

1 回の実行で 1 台分。読み込み後の常駐メモリがコンテナのメモリ（standard-4 は 12 GiB）に収まるかを見る。
メモリは Windows の作業セット（このプロセスの物理メモリ）。読み込み直後と、到達圏を 1 回引いたあとの最大値を出す。

    uv run python scripts/17_measure_shard.py --case world_eurasia --lat 48.8566 --lon 2.3522
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.graph import BASE, RoadGraph, prebuilt_dir  # noqa: E402


def mem_gib():
    """(今の作業セット, これまでの最大) GiB。Windows だけ"""
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
    m = PMC(cb=ctypes.sizeof(PMC))
    psapi = ctypes.WinDLL('psapi')
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(m), m.cb)
    return m.WorkingSetSize / 2**30, m.PeakWorkingSetSize / 2**30


ap = argparse.ArgumentParser()
ap.add_argument('--case', required=True)
ap.add_argument('--speed', default='legal')
ap.add_argument('--lat', type=float, required=True)
ap.add_argument('--lon', type=float, required=True)
ap.add_argument('--limit', type=float, default=120)
a = ap.parse_args()

pre = prebuilt_dir(BASE / 'network' / a.case, a.speed)
disk = sum(p.stat().st_size for p in pre.glob('*.npy')) / 2**30
mmap = sum((pre / f'{n}.npy').stat().st_size for n in ('coords', 'coord_offsets')) / 2**30
dist = sum((pre / f'{n}.npy').stat().st_size for n in ('w_dist_exp', 'w_dist_noexp')) / 2**30
base = mem_gib()[0]
t0 = time.perf_counter()
g = RoadGraph(a.case, speed=a.speed)
load = time.perf_counter() - t0
after_load = mem_gib()
r = g.reachability(a.lat, a.lon, a.limit)
after_reach = mem_gib()
print(f'{a.case} [{a.speed}]: リンク {g.n_links:,}・ノード {g.n_nodes:,}・source {g.source}')
print(f'  ディスク {disk:.2f} GiB（うちメモリマップ {mmap:.2f}・距離の重み {dist:.2f}）')
print(f'  読み込み {load:.1f} 秒・常駐 {after_load[0] - base:.2f} GiB（起動前 {base:.2f}）・最大 {after_load[1]:.2f} GiB')
print(f'  到達圏 {a.limit:.0f} 分: {len(r["link_ids"]):,} リンク・{r["elapsed_ms"]:.0f} ms・その後の最大 {after_reach[1]:.2f} GiB')
