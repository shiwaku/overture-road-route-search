"""全球のネットワークを大陸（つながっている道路網）ごとに 3 つに分ける（issue #9 の A 案）

Cloudflare Containers は 1 台 12 GiB までなので、全球のグラフ（常駐 16 GiB）を分けて別々のコンテナに載せる。
別の道路網（弱連結成分）どうしはもともと経路がつながらないので、成分の単位で分ければ結果は変わらない。

分け方（成分ごと）:
- eurasia: いちばん大きい道路網（ユーラシア・アフリカ）
- americas: それ以外で、ノードの経度の平均が西経 25 度より西（北米・南米・ハワイ・グリーンランドなど）
- other: 残り（日本・グレートブリテン島・オーストラリア・島々）

出力: network/world_<名前>/ に links.parquet・nodes.parquet・legal_speeds.parquet と、
link_gid.npy（その台の link_id → 全球の link_id。画面の道路タイルは全球の link_id を持つので、API はこれで直して返す）。
node_id は台ごとに 0 から振り直す（links は node1 の順のまま）。

成分は組み立て済みの全球グラフの weak.npy を使う（scripts/10_build_graph_arrays.py --case world を先に）。

    uv run python scripts/16_split_world.py
    uv run python scripts/10_build_graph_arrays.py --case world_eurasia --speed legal --no-verify   # americas・other も
"""

import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / 'network' / 'world'
SHARDS = ('eurasia', 'americas', 'other')
AMERICAS_LON = -25.0

t0 = time.perf_counter()
weak = np.load(SRC / 'graph' / 'weak.npy')
lon = pq.read_table(SRC / 'nodes.parquet', columns=['lon'])['lon'].to_numpy()
assert len(weak) == len(lon)
sizes = np.bincount(weak)
mean_lon = np.bincount(weak, weights=lon) / np.maximum(sizes, 1)
comp_shard = np.where(mean_lon < AMERICAS_LON, 1, 2).astype(np.int8)
comp_shard[np.argmax(sizes)] = 0
node_shard = comp_shard[weak]
del weak, lon
# 古い node_id → その台の node_id（台ごとに出てくる順に 0 から）
new_id = np.empty(len(node_shard), dtype=np.int64)
for s in range(len(SHARDS)):
    m = node_shard == s
    new_id[m] = np.arange(m.sum())
    print(f'{SHARDS[s]}: ノード {m.sum():,}・道路網 {np.count_nonzero(comp_shard == s):,}', flush=True)


def split(name, shard_of_row, transform=None):
    """parquet を行ごとの台の番号で 3 つに分けて書く。返り値は台ごとの元の行番号"""
    pf = pq.ParquetFile(SRC / name)
    writers = [pq.ParquetWriter(BASE / 'network' / f'world_{s}' / name, pf.schema_arrow, compression='zstd')
               for s in SHARDS]
    rows = [[] for _ in SHARDS]
    off = 0
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        sh = shard_of_row(t, off)
        for s, w in enumerate(writers):
            m = sh == s
            if not m.any():
                continue
            part = t.filter(pa.array(m))
            if transform:
                part = transform(part)
            w.write_table(part)
            rows[s].append(off + np.flatnonzero(m))
        off += len(t)
    for w in writers:
        w.close()
    return [np.concatenate(r) if r else np.empty(0, dtype=np.int64) for r in rows]


def remap(cols):
    def f(t):
        for c in cols:
            i = t.schema.get_field_index(c)
            t = t.set_column(i, t.schema.field(i), pa.array(new_id[t[c].to_numpy()]))   # not null のまま
        return t
    return f


for s in SHARDS:
    (BASE / 'network' / f'world_{s}').mkdir(parents=True, exist_ok=True)

split('nodes.parquet', lambda t, off: node_shard[t['node_id'].to_numpy()], remap(['node_id']))
print(f'nodes: {time.perf_counter() - t0:.0f} 秒', flush=True)

link_shard = []


dropped = [0]


def by_node1(t, off):
    # 両端が別の台のリンクは、どちらの向きにも通れないリンク（成分は通れる辺だけで数える）。探索に出てこないので落とす
    sh = node_shard[t['node1'].to_numpy()]
    cross = sh != node_shard[t['node2'].to_numpy()]
    if cross.any():
        passable = (pc.fill_null(pc.is_finite(t['time_fwd_s']), False).to_numpy(zero_copy_only=False)
                    | pc.fill_null(pc.is_finite(t['time_bwd_s']), False).to_numpy(zero_copy_only=False))
        assert not (cross & passable).any(), '通れるリンクの両端が別の台'
        sh = np.where(cross, -1, sh)
        dropped[0] += int(cross.sum())
    link_shard.append(sh)
    return sh


gids = split('links.parquet', by_node1, remap(['node1', 'node2']))
print(f'links: {time.perf_counter() - t0:.0f} 秒・どちらにも通れず台をまたぐリンクを {dropped[0]:,} 本落とした', flush=True)
link_shard = np.concatenate(link_shard)
split('legal_speeds.parquet', lambda t, off: link_shard[off:off + len(t)])

for s, g in zip(SHARDS, gids):
    np.save(BASE / 'network' / f'world_{s}' / 'link_gid.npy', g.astype(np.int64))
    print(f'{s}: リンク {len(g):,}（{len(g) / len(link_shard):.1%}）', flush=True)
print(f'完了: {time.perf_counter() - t0:.0f} 秒')
