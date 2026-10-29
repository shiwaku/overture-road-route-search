"""国土数値情報 N13（道路）と Overture を重ねて、N13 の道が Overture にあるかを調べる（PLAN.md 段階 2）

N13 の線に沿って STEP_M ごとに点を打ち、いちばん近い Overture の道（subtype=road、class は問わない）までの距離を測る。
点は「その点が代表する N13 の長さ」を持つので、距離が閾値以内の点の長さを足すと「Overture にある N13 の延長」になる。

- N13 は ../ksj-route-search-api/input/geoparquet/N13-24_<1次メッシュ>.parquet（160 ファイル）
- 1 次メッシュごとに、その範囲の Overture の道を読み、平面（メッシュの南西隅を原点とする正距円筒・m）に直して比べる
- 距離は MAX_DIST_M で打ち切る（それより遠いものは「無い」）
- 集計は 2 次メッシュ（N13_008）× 道路種別（N13_003）× 幅員区分（N13_006）× 距離の区切り × いちばん近い Overture の class

    uv run python scripts/04_coverage_n13.py              # 全国（並列）
    uv run python scripts/04_coverage_n13.py 5339         # 1 次メッシュを指定
出力: output/coverage/n13_match.parquet
"""

import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import shapely

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
JP = BASE / 'data' / 'jp' / RELEASE
N13 = BASE.parent / 'ksj-route-search-api' / 'input' / 'geoparquet'
OUT = BASE / 'output' / 'coverage'
STEP_M = 20
MAX_DIST_M = 50
BINS = [0, 5, 10, 15, 20, 30, 50]        # 距離の区切り（m）。50 m 超は「無い」
WORKERS = 12


def to_local(geoms, lon0, lat0):
    """経緯度 → メッシュの南西隅を原点とする平面（m）"""
    kx = 111_320 * np.cos(np.radians(lat0 + 1 / 3))
    return shapely.transform(geoms, lambda xy: (xy - [lon0, lat0]) * [kx, 110_574])


def run_mesh(mesh1):
    lat0, lon0 = int(mesh1[:2]) / 1.5, int(mesh1[2:]) + 100
    c = duckdb.connect()
    c.sql("LOAD spatial; SET geometry_always_xy=true")
    n13 = c.sql(f"""SELECT N13_003 AS road_type, N13_006 AS width, N13_008 AS mesh2, ST_AsWKB(geometry) AS wkb
        FROM read_parquet('{N13 / f'N13-24_{mesh1}.parquet'}')""").df()
    m = 0.001       # メッシュの外にはみ出した Overture も拾う（約 100 m）
    ov = c.sql(f"""SELECT class, ST_AsWKB(geometry) AS wkb FROM read_parquet('{JP / 'segment.parquet'}')
        WHERE subtype = 'road'
          AND bbox.xmax > {lon0 - m} AND bbox.xmin < {lon0 + 1 + m}
          AND bbox.ymax > {lat0 - m} AND bbox.ymin < {lat0 + 2 / 3 + m}""").df()
    if n13.empty:
        return pd.DataFrame()

    lines = to_local(shapely.from_wkb(n13.wkb.map(bytes)), lon0, lat0)
    length = shapely.length(lines)
    n = np.maximum(np.ceil(length / STEP_M), 1).astype(int)
    li = np.repeat(np.arange(len(lines)), n)
    k = np.arange(len(li)) - np.repeat(np.cumsum(n) - n, n)
    pts = shapely.line_interpolate_point(lines[li], (k + 0.5) / n[li], normalized=True)
    w = length[li] / n[li]

    dist = np.full(len(pts), np.inf)
    cls = np.full(len(pts), None, dtype=object)
    if not ov.empty:
        tree = shapely.STRtree(to_local(shapely.from_wkb(ov.wkb.map(bytes)), lon0, lat0))
        (ip, io), d = tree.query_nearest(pts, max_distance=MAX_DIST_M, return_distance=True, all_matches=False)
        dist[ip] = d
        cls[ip] = ov['class'].to_numpy()[io]

    df = pd.DataFrame({
        'mesh2': n13.mesh2.to_numpy()[li], 'road_type': n13.road_type.to_numpy()[li], 'width': n13.width.to_numpy()[li],
        'dist_bin': pd.cut(dist, BINS + [np.inf], right=True, include_lowest=True, labels=[f'<={b}' for b in BINS[1:]] + ['none']).astype(str),
        'ov_class': cls, 'length_m': w,
    })
    df.loc[df.dist_bin == 'none', 'ov_class'] = None
    return df.groupby(['mesh2', 'road_type', 'width', 'dist_bin', 'ov_class'], dropna=False, as_index=False)['length_m'].sum()


def main():
    meshes = sys.argv[1:] or sorted(p.stem.split('_')[1] for p in N13.glob('N13-24_*.parquet'))
    t0 = time.time()
    out = []
    with ProcessPoolExecutor(WORKERS) as ex:
        for i, (mesh1, df) in enumerate(zip(meshes, ex.map(run_mesh, meshes)), 1):
            out.append(df)
            print(f'[{time.time() - t0:6.0f}s] {i}/{len(meshes)} {mesh1}  N13 {df.length_m.sum() / 1000 if len(df) else 0:,.0f} km', flush=True)
    res = pd.concat(out, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    name = 'n13_match.parquet' if not sys.argv[1:] else f'n13_match_{"_".join(meshes)}.parquet'
    res.to_parquet(OUT / name, index=False)
    tot = res.length_m.sum()
    print(f'\nN13 の延長 {tot / 1000:,.0f} km')
    for b in BINS[1:]:
        ok = res[res.dist_bin.isin([f'<={x}' for x in BINS[1:] if x <= b])].length_m.sum()
        print(f'  {b:>3} m 以内に Overture の道がある: {ok / tot:.1%}')


if __name__ == '__main__':
    main()
