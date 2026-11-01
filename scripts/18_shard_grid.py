"""Worker の振り分け表: 緯度経度 → 担当の台（大陸ごとに分けたグラフ、scripts/16_split_world.py）（issue #9）

全球を 0.1 度のマスに切り、各マスを担当する台を決める。Worker は地点のマスを引いて、その台のコンテナだけを起こす。

1. スナップ先ノード（各台の kd_lat・kd_lon）があるマスは、いちばん多くのノードを持つ台
2. ノードの無いマス（海・砂漠など）は、いちばん近い「ノードのあるマス」の台（マスの格子上の距離。
   scipy の distance_transform_edt）。経度の端（日付変更線）はつながっていないものとして扱う

- 値は台の番号（0 eurasia・1 americas・2 other。deploy/cloudflare/src/worker.ts の SHARDS と同じ順）
- 並びは南から北の行、各行は西から東。行 = floor((lat + 90) / 0.1)、列 = floor((lon + 180) / 0.1)
- 0.1 度は約 11 km。海峡を挟んだ 2 つの台の境目（ドーバー海峡・対馬海峡など）では、地点とマスとで最寄りの台が
  違うことがあり、そのときは担当の台の中でいちばん近いノードにスナップする（スナップの距離が長くなる）

出力: deploy/cloudflare/src/shard_grid.bin（3600 × 1800 バイト・git に入れない）

    uv run python scripts/18_shard_grid.py
"""

import sys
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import distance_transform_edt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.graph import BASE, prebuilt_dir  # noqa: E402

SHARDS = ('eurasia', 'americas', 'other')
STEP = 0.1
NLON, NLAT = round(360 / STEP), round(180 / STEP)
OUT = BASE / 'deploy' / 'cloudflare' / 'src' / 'shard_grid.bin'

t0 = time.perf_counter()
counts = np.zeros((len(SHARDS), NLAT * NLON), dtype=np.int64)
for s, name in enumerate(SHARDS):
    pre = prebuilt_dir(BASE / 'network' / f'world_{name}', 'legal')
    la, lo = np.load(pre / 'kd_lat.npy'), np.load(pre / 'kd_lon.npy')
    r = np.clip(np.floor((la + 90) / STEP).astype(np.int64), 0, NLAT - 1)
    c = np.clip(np.floor((lo + 180) / STEP).astype(np.int64), 0, NLON - 1)
    counts[s] = np.bincount(r * NLON + c, minlength=NLAT * NLON)
    print(f'{name}: スナップ先 {len(la):,} 点・{time.perf_counter() - t0:.0f} 秒', flush=True)
has = counts.sum(axis=0) > 0
owner = np.argmax(counts, axis=0).astype(np.uint8)
mixed = int(np.count_nonzero((counts > 0).sum(axis=0) > 1))
# ノードの無いマスは、いちばん近いノードのあるマスの台
_, (ri, ci) = distance_transform_edt(~has.reshape(NLAT, NLON), return_indices=True)
grid = owner.reshape(NLAT, NLON)[ri, ci].ravel()
print(f'ノードのあるマス {np.count_nonzero(has):,}（うち 2 台のノードが混じるマス {mixed:,}）・{time.perf_counter() - t0:.0f} 秒')

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_bytes(grid.tobytes())
counts = np.bincount(grid, minlength=len(SHARDS))
print('マスの数: ' + '・'.join(f'{n} {c:,}' for n, c in zip(SHARDS, counts)))
print(f'書き出し: {OUT}（{OUT.stat().st_size / 2**20:.1f} MiB）')

# 確かめ: 主な地点の担当
for name, la, lo in [('東京', 35.68, 139.77), ('ソウル', 37.57, 126.98), ('ロンドン', 51.51, -0.13), ('パリ', 48.86, 2.35),
                     ('ニューヨーク', 40.76, -73.99), ('サンパウロ', -23.55, -46.63), ('シドニー', -33.87, 151.21),
                     ('ケープタウン', -33.92, 18.42), ('ホノルル', 21.31, -157.86), ('レイキャビク', 64.15, -21.94)]:
    r, c = int((la + 90) // STEP), int((lo + 180) // STEP)
    print(f'  {name}: {SHARDS[grid[r * NLON + c]]}')
