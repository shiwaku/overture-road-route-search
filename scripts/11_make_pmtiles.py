"""links.parquet → 道路の PMTiles（tippecanoe）（PLAN.md 段階 6）

../ksj-route-search-api の preprocess/make_pmtiles.py を Overture の class 向けに直したもの。
到達圏 API はジオメトリを返さず link_id を返すので、画面はこのタイルに setFeatureState で色を塗る。

- link_id（links.parquet の行番号）をフィーチャ ID にする（--use-attribute-for-id）。
  tippecanoe は ID 0 を表せないので、link_id = 0 の 1 本だけはタイル上で ID を持たない（KSJ 版と同じ割り切り）
- 属性は road_type（1=motorway 2=trunk 3=primary 4=secondary 5=tertiary）だけ
- ズームごとに道路の種類を出し分ける（低ズームに細い道まで入れると間引かれて点々になる）:
    z4 まで motorway / z5-6 ＋trunk / z7-8 ＋primary / z9-10 ＋secondary / z11- 全部

    uv run python scripts/11_make_pmtiles.py --case jp --wsl     # Windows では WSL の tippecanoe を使う
    uv run python scripts/11_make_pmtiles.py --case world --wsl --bbox 122,24,154,46 --name jp
        # 全球のネットワークから範囲を切り出す（link_id は全球のまま）→ roads_world_jp.pmtiles
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely

BASE = Path(__file__).resolve().parent.parent
ROAD_TYPE = {'motorway': 1, 'trunk': 2, 'primary': 3, 'secondary': 4, 'tertiary': 5}

ap = argparse.ArgumentParser()
ap.add_argument('--case', required=True)
ap.add_argument('--layer', default='roads')
ap.add_argument('--min-zoom', type=int, default=2)
ap.add_argument('--max-zoom', type=int, default=13)
ap.add_argument('--precision', type=int, default=6, help='座標の小数桁（6 桁 ≒ 11 cm）')
ap.add_argument('--max-tile-bytes', type=int, default=1_500_000)
ap.add_argument('--wsl', action='store_true', help='tippecanoe を WSL で動かす（Windows 用）')
ap.add_argument('--bbox', help='この範囲（west,south,east,north）に掛かるリンクだけ出す')
ap.add_argument('--name', help='出力名に付ける（roads_<case>_<name>.pmtiles）')
a = ap.parse_args()

d = BASE / 'network' / a.case
src = d / 'links.parquet'
out = d / (f'roads_{a.case}_{a.name}.pmtiles' if a.name else f'roads_{a.case}.pmtiles')
bbox = [float(v) for v in a.bbox.split(',')] if a.bbox else None

pf = pq.ParquetFile(src)
total = pf.metadata.num_rows
print(f'入力: {src}  {total:,} リンク / row group {pf.num_row_groups} 個', flush=True)

ZOOM_FILTER = json.dumps({a.layer: [
    'any',
    ['>=', '$zoom', 11],
    ['all', ['>=', '$zoom', 9], ['<=', 'road_type', 4]],
    ['all', ['>=', '$zoom', 7], ['<=', 'road_type', 3]],
    ['all', ['>=', '$zoom', 5], ['<=', 'road_type', 2]],
    ['==', 'road_type', 1],
]})


def to_wsl(path):
    p = Path(path).resolve()
    return f'/mnt/{p.drive[0].lower()}' + p.as_posix()[len(p.drive):]


cmd = [
    # wsl -e はシェルを通さない（通すと ZOOM_FILTER の $zoom が空に展開される）
    *(['wsl', '-e', 'tippecanoe', '-o', to_wsl(out)] if a.wsl else ['tippecanoe', '-o', str(out)]), '-l', a.layer,
    '-Z', str(a.min_zoom), '-z', str(a.max_zoom),
    '--use-attribute-for-id=link_id',
    '-j', ZOOM_FILTER,
    f'--maximum-tile-bytes={a.max_tile_bytes}',
    '--drop-densest-as-needed',
    '--extend-zooms-if-still-dropping',
    '--simplification=10', '--simplification-at-maximum-zoom=4', '-D', '10',
    '--force', '-q',
]
print('  ' + ' '.join(cmd), flush=True)

t0 = time.time()
p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
w = p.stdin
done = 0
try:
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['geometry', 'class'])
        geom = shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False))
        ids = np.arange(done, done + len(geom))               # ← 行番号 = link_id（切り出しても変えない）
        rt = np.array([ROAD_TYPE[c] for c in t.column('class').to_pylist()])
        done += len(geom)
        if bbox:
            b = shapely.bounds(geom)
            keep = (b[:, 2] >= bbox[0]) & (b[:, 0] <= bbox[2]) & (b[:, 3] >= bbox[1]) & (b[:, 1] <= bbox[3])
            geom, ids, rt = geom[keep], ids[keep], rt[keep]
        geom = shapely.transform(geom, lambda c: np.round(c, a.precision))
        gj = shapely.to_geojson(geom)
        w.write(''.join(
            f'{{"type":"Feature","properties":{{"link_id":{i},"road_type":{r}}},"geometry":{g}}}\n'
            for i, r, g in zip(ids, rt, gj)).encode())
        if rg % 20 == 0 or done == total:
            print(f'  {done:,} / {total:,} ({done / total * 100:.0f}%)  {time.time() - t0:.0f}s', flush=True)
    w.close()
except BrokenPipeError:
    print('tippecanoe が終了しました', file=sys.stderr)

rc = p.wait()
if rc != 0:
    sys.exit(f'tippecanoe が異常終了しました (rc={rc})')
print(f'\n完了: {out}  {out.stat().st_size / 1e6:,.0f} MB  {(time.time() - t0) / 60:.1f} 分', flush=True)
