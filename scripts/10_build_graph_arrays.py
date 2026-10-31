"""リンク・ノードの parquet → 組み立て済みの探索グラフ（network/<case>/graph/*.npy と meta.json）（PLAN.md 段階 5）

API は起動時にこれがあれば読むだけになる（../ksj-route-search-api の preprocess/build_graph_arrays.py と同じ役割）。
parquet を作り直したら、これも作り直す（meta.json の parquet の大きさが違えば API は parquet から組み立てる）。

    uv run python scripts/10_build_graph_arrays.py --case jp
    uv run python scripts/10_build_graph_arrays.py --case world --no-verify --speed class   # → network/world/graph_class
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.graph import BASE, SPEEDS, RoadGraph, prebuilt_dir  # noqa: E402


def peak_gib():
    """このプロセスがこれまでに使ったメモリ（物理）の最大値 GiB。Windows だけ（他は None）"""
    if sys.platform != 'win32':
        return None
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
    return m.PeakWorkingSetSize / 2**30


ap = argparse.ArgumentParser()
ap.add_argument('--case', default='jp')
ap.add_argument('--speed', choices=SPEEDS, default='posted', help='posted: 制限速度を使う / class: class の既定値だけ')
ap.add_argument('--no-verify', action='store_true', help='読み直して配列を比べるのを飛ばす（全球ではメモリを 2 倍使う）')
args = ap.parse_args()

out = prebuilt_dir(BASE / 'network' / args.case, args.speed)
t0 = time.perf_counter()
g = RoadGraph(args.case, prebuilt=False, speed=args.speed)
print(f'parquet から組み立て: {g.load_seconds:.1f} 秒（links {g.n_links:,} / nodes {g.n_nodes:,}）', flush=True)
g.save(out)
size = sum(p.stat().st_size for p in out.glob('*.npy'))
print(f'書き出し: {out}（{size / 2**30:,.2f} GiB）', flush=True)
if (pk := peak_gib()) is not None:
    print(f'組み立て中のメモリの最大: {pk:,.1f} GiB', flush=True)

if not args.no_verify:
    g2 = RoadGraph(args.case, speed=args.speed)
    assert g2.source == 'prebuilt', '組み立て済みが使われなかった（meta.json を確認）'
    for name, a in g._arrays().items():
        assert np.array_equal(a, g2._arrays()[name], equal_nan=a.dtype.kind == 'f'), name   # NaN = 通れない向き
    print(f'読み直し: {g2.load_seconds:.1f} 秒・全配列一致（合計 {time.perf_counter() - t0:.0f} 秒）')
