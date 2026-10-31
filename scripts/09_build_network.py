"""Overture の segment から車の道路ネットワーク（リンクとノードの parquet）を作る（PLAN.md 段階 4）

1. 入力ファイルごとに（全球は 128 ファイル。1 つずつ処理してメモリを抑える）
   - 幹線（motorway〜tertiary・ランプを含む）だけを残し、車が通れない区間を外す
   - segment を connectors[].at の位置で区切ってリンクにする。区切る位置は at に最も近い頂点
     （Overture の connector はほぼ頂点の上にある）。始点より前・終点より後のはみ出しは捨てる
   - 一方通行: access_restrictions の heading 付き denied（車に当てはまるもの）
   - 速度: 無条件の制限速度（when・between が無い max_speed）があればそれ、無ければ class ごとの既定値
   → network/<case>/parts/NNN.parquet（ノードは connector の id のまま）
2. connector の id に通し番号を振ってノードにする。番号はノードの位置の Hilbert 曲線の順
   （近いノードが CSR の中でも近くに並び、探索が速くなる）。リンクは node1 の順に並べる
   → network/<case>/links.parquet・nodes.parquet

リンクの行番号を link_id とする（ksj-route-search-api と同じ約束）。

割り切り:
- 区間の一部だけに掛かる規制・制限速度（between 付き）、曜日や時間帯で変わる規制（during 付き）は見ない
- 右左折の禁止（prohibited_transitions）は見ない
- 幹線どうしは connector でつながっていても、間に幹線以外の道しか無ければつながらない

    uv run python scripts/09_build_network.py --case jp       # 日本（data/jp の segment.parquet）
    uv run python scripts/09_build_network.py --case world    # 全球（data/world の segment/*.parquet）
"""

import argparse
import shutil
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
INPUTS = {
    'jp': [BASE / 'data' / 'jp' / RELEASE / 'segment.parquet'],
    'world': sorted((BASE / 'data' / 'world' / RELEASE / 'segment').glob('*.parquet')),
}
CLASSES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
# 制限速度が無い区間の速度 km/h（PLAN.md ②）。ランプは class ごとに別の値
SPEED_KMH = {'motorway': 80, 'trunk': 50, 'primary': 40, 'secondary': 35, 'tertiary': 30}
SPEED_LINK_KMH = {'motorway': 45, 'trunk': 30, 'primary': 30, 'secondary': 25, 'tertiary': 20}
CAR_MODES = "['vehicle', 'motor_vehicle', 'car']"
EARTH_R = 6_371_008.8

ap = argparse.ArgumentParser()
ap.add_argument('--case', choices=INPUTS, required=True)
ap.add_argument('--limit', type=int, help='最初の N ファイルだけ（試すとき）')
ap.add_argument('--memory', default='16GB', help='DuckDB のメモリ上限（超えた分は一時ファイルに逃がす）')
ap.add_argument('--threads', type=int, default=8, help='DuckDB のスレッド数')
a = ap.parse_args()
OUT = BASE / 'network' / a.case
PARTS = OUT / 'parts'
PARTS.mkdir(parents=True, exist_ok=True)

c = duckdb.connect()
c.sql('LOAD spatial; SET geometry_always_xy=true')
# DuckDB の既定はメモリの 8 割まで使う。全球のリンクの並べ替えで PC 全体がメモリ不足にならないよう抑える
c.sql(f"SET memory_limit='{a.memory}'; SET threads={a.threads}; SET temp_directory='{(OUT / 'duckdb_tmp').as_posix()}'")
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


# 車に当てはまる無条件の規制（時間帯・区間・利用者・車両の大きさの条件が無く、mode が無いか車を含む）
CAR_RULE = f"""(r."when" IS NULL OR (r."when".during IS NULL AND r."when"."using" IS NULL
    AND r."when".recognized IS NULL AND r."when".vehicle IS NULL
    AND (r."when"."mode" IS NULL OR list_has_any(r."when"."mode", {CAR_MODES}))))
    AND r."between" IS NULL"""
HEADING = 'CASE WHEN r."when" IS NULL THEN NULL ELSE r."when".heading END'


def read_segments(path):
    """幹線で車が通れる segment の属性。geometry は WKB"""
    return c.sql(f"""
        WITH s AS (
            SELECT id, class, coalesce(subclass = 'link', false) AS is_link, connectors,
                ST_AsWKB(geometry) AS wkb,
                len(list_filter(access_restrictions, lambda r: r.access_type = 'denied' AND {CAR_RULE}
                    AND {HEADING} IS NULL)) > 0
                AND len(list_filter(access_restrictions, lambda r: r.access_type IN ('allowed', 'designated')
                    AND r."when"."mode" IS NOT NULL AND list_has_any(r."when"."mode", {CAR_MODES}))) = 0 AS denied,
                len(list_filter(access_restrictions, lambda r: r.access_type = 'denied' AND {CAR_RULE}
                    AND {HEADING} = 'forward')) > 0 AS no_fwd,
                len(list_filter(access_restrictions, lambda r: r.access_type = 'denied' AND {CAR_RULE}
                    AND {HEADING} = 'backward')) > 0 AS no_bwd,
                list_filter(speed_limits, lambda r: r.max_speed IS NOT NULL AND r."when" IS NULL
                    AND r."between" IS NULL)[1].max_speed AS ms
            FROM read_parquet('{path}')
            WHERE subtype = 'road' AND class IN {CLASSES} AND len(connectors) >= 2
        )
        -- access_restrictions が NULL の segment では判定も NULL になるので false に倒す
        SELECT id, class, is_link, connectors, wkb, coalesce(no_fwd, false) AS no_fwd, coalesce(no_bwd, false) AS no_bwd,
            CASE ms.unit WHEN 'mph' THEN ms."value" * 1.609344 ELSE ms."value" END AS maxspeed_kmh
        FROM s WHERE NOT coalesce(denied, false)""").to_arrow_table()


def split(t):
    """segment → リンク。頂点の測地線長の累積で at を頂点に寄せ、隣り合う connector の間を 1 本のリンクにする"""
    geoms = shapely.from_wkb(t['wkb'].to_numpy(zero_copy_only=False))
    xy, gi = shapely.get_coordinates(geoms, return_index=True)
    # 頂点ごとの累積の長さ（haversine）。segment の先頭の頂点で 0 に戻す
    lon, lat = np.radians(xy[:, 0]), np.radians(xy[:, 1])
    h = (np.sin(np.diff(lat) / 2) ** 2
         + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2)
    step = np.concatenate([[0.0], 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(h, 0, 1)))])
    first = np.concatenate([[True], gi[1:] != gi[:-1]])
    step[first] = 0
    cum = np.cumsum(step)
    start = np.flatnonzero(first)                     # segment ごとの先頭の頂点
    cum -= np.repeat(cum[start], np.diff(np.append(start, len(cum))))
    total = np.repeat(cum[np.append(start[1:], len(cum)) - 1], np.diff(np.append(start, len(cum))))
    frac = np.divide(cum, total, out=np.zeros_like(cum), where=total > 0)

    # connector を展開して (segment, at) の順に並べる
    conn = t['connectors'].combine_chunks()
    n_conn = np.diff(conn.offsets.to_numpy())
    cseg = np.repeat(np.arange(len(t)), n_conn)
    flat = conn.flatten()
    cid = flat.field('connector_id').to_numpy(zero_copy_only=False)
    at = np.clip(flat.field('at').fill_null(0).to_numpy(), 0, 1)
    order = np.lexsort((at, cseg))
    cseg, cid, at = cseg[order], cid[order], at[order]

    # at に最も近い頂点。キーを segment × 2 ＋ 割合にすると全体で 1 本の昇順の列になる
    vkey = gi * 2.0 + frac
    ckey = cseg * 2.0 + at
    j = np.clip(np.searchsorted(vkey, ckey), 1, len(vkey) - 1)
    left_closer = (ckey - vkey[j - 1]) <= (vkey[j] - ckey)
    v = np.where(left_closer, j - 1, j)
    seg_first, seg_last = start[cseg], np.append(start[1:], len(cum))[cseg] - 1
    v = np.clip(v, seg_first, seg_last)

    # 同じ segment の中で隣り合う connector の間がリンク
    same = cseg[1:] == cseg[:-1]
    k = np.flatnonzero(same & (cid[1:] != cid[:-1]))  # 同じ connector で閉じる輪は捨てる
    seg = cseg[k]
    vs, ve = v[k], v[k + 1]
    dist = cum[ve] - cum[vs]

    # リンクの形: 頂点 vs〜ve（両端を含む）。2 つの connector が同じ頂点に寄ったら、同じ点を 2 つ並べた長さ 0 の線
    n_pts = np.maximum(ve - vs + 1, 2)
    pt_idx = np.repeat(vs - np.cumsum(np.concatenate([[0], n_pts[:-1]])), n_pts) + np.arange(n_pts.sum())
    pt_idx = np.minimum(pt_idx, np.repeat(ve, n_pts))
    lines = shapely.linestrings(xy[pt_idx], indices=np.repeat(np.arange(len(k)), n_pts))

    cls = t['class'].to_numpy(zero_copy_only=False)[seg]
    is_link = t['is_link'].to_numpy(zero_copy_only=False)[seg]
    ms = t['maxspeed_kmh'].to_numpy(zero_copy_only=False).astype(float)[seg]
    default = np.where(is_link, [SPEED_LINK_KMH[x] for x in cls], [SPEED_KMH[x] for x in cls])
    has_ms = np.isfinite(ms) & (ms > 0)
    speed = np.where(has_ms, ms, default)
    time_s = dist / (speed / 3.6)
    no_fwd = t['no_fwd'].to_numpy(zero_copy_only=False)[seg]
    no_bwd = t['no_bwd'].to_numpy(zero_copy_only=False)[seg]
    return pa.table({
        'segment_id': t['id'].take(pa.array(seg)),
        'c1': cid[k], 'c2': cid[k + 1],
        'x1': xy[vs, 0], 'y1': xy[vs, 1], 'x2': xy[ve, 0], 'y2': xy[ve, 1],
        'class': cls, 'is_link': is_link,
        'oneway': no_fwd ^ no_bwd,
        'speed_kmh': speed.astype(np.float32), 'speed_posted': has_ms,
        'dist_m': dist.astype(np.float32),
        'time_fwd_s': np.where(no_fwd, np.nan, time_s).astype(np.float32),     # NaN = その向きには通れない
        'time_bwd_s': np.where(no_bwd, np.nan, time_s).astype(np.float32),
        'geometry': shapely.to_wkb(lines),
    })


# --- 1. 入力ファイルごと
inputs = INPUTS[a.case][:a.limit]
el(f'{len(inputs)} ファイル')
for i, path in enumerate(inputs):
    p = PARTS / f'{i:03d}.parquet'
    if p.exists():
        continue
    t = read_segments(path)
    links = split(t)
    tmp = p.with_suffix('.tmp')
    pq.write_table(links, tmp, compression='zstd')
    tmp.rename(p)
    el(f'  {i + 1:3d}/{len(inputs)}  segment {len(t):,} → リンク {len(links):,}')

# --- 2. ノードに通し番号を振る
el('ノードに通し番号を振る（Hilbert 曲線の順）')
c.sql('SET preserve_insertion_order=false')
parts = f"read_parquet('{PARTS}/*.parquet')"
c.sql(f"""CREATE TABLE node AS
    WITH e AS (
        SELECT c1 AS cid, x1 AS x, y1 AS y FROM {parts}
        UNION ALL SELECT c2, x2, y2 FROM {parts}),
    u AS (SELECT cid, any_value(x) AS x, any_value(y) AS y FROM e GROUP BY cid)
    SELECT row_number() OVER (ORDER BY ST_Hilbert(x, y, {{'min_x': -180, 'min_y': -90, 'max_x': 180, 'max_y': 90}}::BOX_2D)) - 1
        AS node_id, cid, x, y FROM u""")
c.sql(f"""COPY (SELECT node_id, cid AS connector_id, x AS lon, y AS lat, ST_Point(x, y) AS geometry FROM node ORDER BY node_id)
    TO '{OUT / 'nodes.parquet'}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
el(f'  {c.sql("SELECT count(*) FROM node").fetchall()[0][0]:,} ノード')

el('リンクに node1 / node2 を付けて node1 の順に並べる')
c.sql(f"""COPY (
    SELECT n1.node_id AS node1, n2.node_id AS node2, l.segment_id, l.class, l.is_link, l.oneway,
        l.speed_kmh, l.speed_posted, l.dist_m, l.time_fwd_s, l.time_bwd_s, ST_GeomFromWKB(l.geometry) AS geometry
    FROM {parts} l JOIN node n1 ON l.c1 = n1.cid JOIN node n2 ON l.c2 = n2.cid
    ORDER BY n1.node_id, n2.node_id
) TO '{OUT / 'links.parquet'}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
s = c.sql(f"""SELECT count(*), sum(dist_m) / 1000, avg(oneway::int), avg(speed_posted::int)
    FROM read_parquet('{OUT / 'links.parquet'}')""").fetchall()[0]
el(f'  {s[0]:,} リンク・{s[1]:,.0f} km・一方通行 {s[2]:.1%}・制限速度あり {s[3]:.1%}')
for f in ('links.parquet', 'nodes.parquet'):
    print(f'  {(OUT / f).relative_to(BASE)}  {(OUT / f).stat().st_size / 1e9:.2f} GB')
el('完了')
