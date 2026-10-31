"""
道路グラフ（in-memory・有向）

../ksj-route-search-api の api/graph.py を Overture の全球の道路ネットワーク向けに直したもの。
探索は同じく scipy の Dijkstra で、グラフはプロセスに常駐させる。

【KSJ 版からの変更】
- 有向グラフ。一方通行のリンクは片方向の重みしか持たない（KSJ には一方通行の属性が無かった）
- スナップ先は「**強**連結成分が 1,000 ノード以上」のノード。弱連結で数えると、一方通行の行き止まりに
  入り込むノード（名古屋駅前など）に寄ってしまい、どこにも行けない
- KD 木は単位球面上の 3 次元座標で引く。緯度経度のままだと高緯度と日付変更線の近くで最寄りを誤る
- 到達圏は全リンクを走査せず、到達したノードにつながるリンクだけを見る（全球では全リンクの走査だけで重い）
- 重みは分（time）とメートル（dist）の float64（scipy は float64 以外だと探索のたびに全体を写す）

【速度は 2 通り】（speed）
- posted: links.parquet の所要時間そのまま（制限速度があればその値、無ければ class ごとの既定値）
- class: 制限速度を使わず、全リンクを class ごとの既定値（SPEED_KMH・SPEED_LINK_KMH）で走る。
  制限速度の入り方は国でまちまちなので、都市どうしを同じ条件で比べるときに使う。通れる向きは posted と同じ
- legal: 制限速度があればその値、無ければその国（州）の法定速度（scripts/14_legal_speeds.py が作る
  network/<case>/legal_speeds.parquet）、それも無ければ class の既定値。データの抜けを法律の値で埋めたもの
組み立て済みの配列は posted が network/<case>/graph、ほかは network/<case>/graph_<speed>。

【link_id は links.parquet の行番号】（KSJ 版と同じ約束）
links.parquet は node1 の順に並んでいる（scripts/09_build_network.py）。node_id は nodes.parquet の行番号。
"""

import json
import math
import time
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import KDTree

BASE = Path(__file__).resolve().parent.parent
EARTH_R = 6_371_008.8

EXPRESSWAY = ('motorway',)
ROAD_CLASS = {'trunk': ('motorway', 'trunk', 'primary'), 'major': ('motorway', 'trunk')}
ROAD_CLASS_ORDER = ('all', 'trunk', 'major')          # auto はこの順に試す

MIN_COMPONENT_NODES = 1_000     # 強連結成分がこれ未満のノードにはスナップしない
TIER_OK_MAX = 400_000
LINKS_HARD_MAX = 1_000_000
# /route の探索上限の初期値 = 直線距離 km × これ ＋ 15 分。届かなければ倍にして ROUTE_LIMIT_TRIES 回まで試す（KSJ 版の値）
ROUTE_MIN_PER_KM = {True: 1.4, False: 3.3}
ROUTE_LIMIT_TRIES = 3
# 制限速度が無い区間の速度 km/h（PLAN.md ②）。ランプは class ごとに別の値。scripts/09_build_network.py もこれを使う
SPEED_KMH = {'motorway': 80, 'trunk': 50, 'primary': 40, 'secondary': 35, 'tertiary': 30}
SPEED_LINK_KMH = {'motorway': 45, 'trunk': 30, 'primary': 30, 'secondary': 25, 'tertiary': 20}
SPEEDS = ('posted', 'class', 'legal')

PREBUILT_FORMAT = 1
PREBUILT_ARRAYS = ('indices', 'indptr', 'starts', 'entry_link', 'w_time_exp', 'w_time_noexp', 'w_dist_exp',
                   'w_dist_noexp', 'i1', 'i2', 'fwd', 'bwd', 'is_expressway', 'mask_trunk', 'mask_major',
                   'link_dist', 'link_time_fwd', 'link_time_bwd', 'ptr1', 'by2', 'ptr2',
                   'weak', 'strong', 'kd_idx', 'kd_lat', 'kd_lon', 'coords', 'coord_offsets')
MMAP_ARRAYS = ('coords', 'coord_offsets')    # 経路の形を出すときしか触らないので読み込まずにメモリマップで持つ


class TooManyLinks(Exception):
    def __init__(self, applied, counts):
        self.applied, self.counts = applied, counts
        alt = '、'.join(f'road_class={k} なら {v:,} 本' for k, v in counts.items() if v <= LINKS_HARD_MAX)
        super().__init__(f'到達リンクが上限 {LINKS_HARD_MAX:,} 本を超えました（{counts[applied]:,} 本）'
                         + (f'。{alt}' if alt else ''))


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def _ranges(ptr, ids):
    """ptr[ids[k]]〜ptr[ids[k]+1] の区間をすべてつなげた添字"""
    lo, hi = ptr[ids], ptr[ids + 1]
    n = hi - lo
    if n.sum() == 0:
        return np.empty(0, dtype=np.int64)
    return np.repeat(lo - np.cumsum(np.concatenate([[0], n[:-1]])), n) + np.arange(n.sum())


def _load_geometry(path):
    """links.parquet の geometry 列 → (座標 float32 [N,2] (lon,lat), リンクごとの offsets [L+1])"""
    import pyarrow.parquet as pq
    import shapely
    pf = pq.ParquetFile(path)
    chunks, counts = [], []
    for rg in range(pf.num_row_groups):
        wkb = pf.read_row_group(rg, columns=['geometry']).column(0).to_numpy(zero_copy_only=False)
        geoms = shapely.from_wkb(wkb)
        xy, idx = shapely.get_coordinates(geoms, return_index=True)
        chunks.append(xy.astype(np.float32))
        counts.append(np.bincount(idx, minlength=len(geoms)))
    coords = np.concatenate(chunks)
    offsets = np.concatenate([[0], np.cumsum(np.concatenate(counts))]).astype(np.int64)
    return coords, offsets


def _parquet_paths(d):
    return d / 'links.parquet', d / 'nodes.parquet'


def prebuilt_dir(d, speed='posted'):
    return d / ('graph' if speed == 'posted' else f'graph_{speed}')


def _source_signature(d):
    return {p.name: p.stat().st_size for p in _parquet_paths(d) if p.exists()}


class RoadGraph:
    """探索グラフ。起動時に 1 回作る。組み立て済みの配列（network/<case>/graph）があれば読むだけ"""

    def __init__(self, case='jp', net_dir='network', prebuilt=True, speed='posted'):
        t0 = time.perf_counter()
        assert speed in SPEEDS, speed
        d = BASE / net_dir / case
        self.case, self.speed = case, speed
        pre = prebuilt_dir(d, speed)
        if prebuilt and self._prebuilt_usable(pre, d, speed):
            self._load_prebuilt(pre)
            self.source = 'prebuilt'
        else:
            self._build(d)
            self.source = 'parquet'
        self._finish()
        self.load_seconds = time.perf_counter() - t0

    # ------------------------------------------------------------------ 組み立て
    def _build(self, d):
        import pandas as pd
        link_path, node_path = _parquet_paths(d)
        import pyarrow.parquet as pq
        # class は辞書（カテゴリ）で読む。文字列のままだと全球の 7 千万本で数 GB になる
        links = pq.read_table(link_path, columns=['node1', 'node2', 'dist_m', 'time_fwd_s', 'time_bwd_s', 'class', 'is_link',
                                                       'speed_posted'],
                              read_dictionary=['class']).to_pandas()
        nodes = pd.read_parquet(node_path, columns=['lon', 'lat'])
        self.n_links, self.n_nodes = len(links), len(nodes)
        NN = self.n_nodes

        self.i1 = links['node1'].to_numpy().astype(np.int32)
        self.i2 = links['node2'].to_numpy().astype(np.int32)
        assert np.all(np.diff(self.i1) >= 0), 'links.parquet が node1 の順に並んでいない'
        cls = links['class']
        self.is_expressway = cls.isin(EXPRESSWAY).to_numpy()
        self._mask_trunk = cls.isin(ROAD_CLASS['trunk']).to_numpy()
        self._mask_major = cls.isin(ROAD_CLASS['major']).to_numpy()
        self.link_dist = links['dist_m'].to_numpy().astype(np.float64)
        self.link_time_fwd = links['time_fwd_s'].to_numpy().astype(np.float64) / 60   # 分。NaN = 通れない
        self.link_time_bwd = links['time_bwd_s'].to_numpy().astype(np.float64) / 60
        if self.speed in ('class', 'legal'):
            # class: 制限速度を捨てて class の既定値で走る。legal: 制限速度が無いリンクだけ法定速度にする。
            # 通れない向き（NaN）はそのまま
            kmh = np.where(links['is_link'].to_numpy(), cls.map(SPEED_LINK_KMH).to_numpy(dtype=np.float64),
                           cls.map(SPEED_KMH).to_numpy(dtype=np.float64))
            if self.speed == 'legal':
                legal = pq.read_table(d / 'legal_speeds.parquet', columns=['legal_kmh'])['legal_kmh'].to_numpy()
                assert len(legal) == self.n_links, 'legal_speeds.parquet が links.parquet と合わない（14 を作り直す）'
                use = ~links['speed_posted'].to_numpy() & np.isfinite(legal)
                kmh = np.where(use, legal, kmh)
            t = self.link_dist / (kmh / 3.6) / 60
            if self.speed == 'legal':
                posted = links['speed_posted'].to_numpy()
                t = np.where(posted, np.fmin(self.link_time_fwd, self.link_time_bwd), t)   # 制限速度のあるリンクは元の時間
            self.link_time_fwd = np.where(np.isfinite(self.link_time_fwd), t, np.nan)
            self.link_time_bwd = np.where(np.isfinite(self.link_time_bwd), t, np.nan)
            del kmh, t
        self.fwd = np.isfinite(self.link_time_fwd)
        self.bwd = np.isfinite(self.link_time_bwd)

        # --- 有向の辺（順方向に通れるリンク → i1→i2、逆方向 → i2→i1）を (行, 列) の順に並べて CSR にする
        lid = np.arange(self.n_links, dtype=np.int32)
        rows = np.concatenate([self.i1[self.fwd], self.i2[self.bwd]])
        cols = np.concatenate([self.i2[self.fwd], self.i1[self.bwd]])
        elink = np.concatenate([lid[self.fwd], lid[self.bwd]])
        etime = np.concatenate([self.link_time_fwd[self.fwd], self.link_time_bwd[self.bwd]])
        del lid
        key = rows.astype(np.int64) * NN + cols
        del rows, cols
        order = np.argsort(key, kind='stable')
        ks = key[order]
        del key
        # 並行リンク（同じ向きに同じ 2 ノードを結ぶリンク）は 1 つの辺にまとめ、重みは最小値。
        # 辺 j の元のリンクは entry_link[starts[j]:starts[j+1]]（経路のリンクを引くときに使う）
        self._starts = np.concatenate([[0], np.flatnonzero(ks[1:] != ks[:-1]) + 1]).astype(np.int64)
        kuni = ks[self._starts]
        del ks
        self._indices = (kuni % NN).astype(np.int32)
        self._indptr = np.concatenate([[0], np.cumsum(np.bincount(kuni // NN, minlength=NN))]).astype(np.int64)
        del kuni
        self._entry_link = elink[order]
        etime = etime[order]
        del order, elink
        edist = self.link_dist[self._entry_link]
        exp = self.is_expressway[self._entry_link]
        # 長さ 0 のリンクも辺として残すため、重みに下限を付ける（scipy は重み 0 の辺を辺と見なさないことがある）
        self._w = {}
        for cost, w in (('time', etime), ('dist', edist)):
            w = np.maximum(w, 1e-6)
            self._w[(cost, True)] = np.minimum.reduceat(w, self._starts)
            self._w[(cost, False)] = np.minimum.reduceat(np.where(exp, np.inf, w), self._starts)
        del etime, edist, exp

        # --- 到達圏用: ノード → そのノードを node1 / node2 に持つリンク
        self.ptr1 = np.searchsorted(self.i1, np.arange(NN + 1)).astype(np.int64)
        self.by2 = np.argsort(self.i2, kind='stable').astype(np.int32)
        self.ptr2 = np.searchsorted(self.i2[self.by2], np.arange(NN + 1)).astype(np.int64)

        # --- 連結成分。弱は「別の道路網」の判定、強はスナップ先の判定
        G = csr_matrix((self._w[('time', True)], self._indices, self._indptr), shape=(NN, NN))
        _, self.weak = connected_components(G, directed=True, connection='weak')
        _, self.strong = connected_components(G, directed=True, connection='strong')
        self.weak, self.strong = self.weak.astype(np.int32), self.strong.astype(np.int32)
        eligible = np.bincount(self.strong)[self.strong] >= MIN_COMPONENT_NODES
        self.kd_idx = np.flatnonzero(eligible).astype(np.int32)
        self.kd_lat = nodes['lat'].to_numpy()[eligible]
        self.kd_lon = nodes['lon'].to_numpy()[eligible]

        self.coords, self.coord_offsets = _load_geometry(link_path)
        self._source = _source_signature(d)

    # ------------------------------------------------------------------ 組み立て済みの保存・読み込み
    def _arrays(self):
        return {
            'indices': self._indices, 'indptr': self._indptr, 'starts': self._starts, 'entry_link': self._entry_link,
            'w_time_exp': self._w[('time', True)], 'w_time_noexp': self._w[('time', False)],
            'w_dist_exp': self._w[('dist', True)], 'w_dist_noexp': self._w[('dist', False)],
            'i1': self.i1, 'i2': self.i2, 'fwd': self.fwd, 'bwd': self.bwd, 'is_expressway': self.is_expressway,
            'mask_trunk': self._mask_trunk, 'mask_major': self._mask_major,
            'link_dist': self.link_dist, 'link_time_fwd': self.link_time_fwd, 'link_time_bwd': self.link_time_bwd,
            'ptr1': self.ptr1, 'by2': self.by2, 'ptr2': self.ptr2,
            'weak': self.weak, 'strong': self.strong,
            'kd_idx': self.kd_idx, 'kd_lat': self.kd_lat, 'kd_lon': self.kd_lon,
            'coords': self.coords, 'coord_offsets': self.coord_offsets,
        }

    def save(self, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, a in self._arrays().items():
            np.save(out / f'{name}.npy', np.ascontiguousarray(a))
        meta = {'format': PREBUILT_FORMAT, 'case': self.case, 'speed': self.speed, 'n_links': self.n_links,
                'n_nodes': self.n_nodes, 'source': self._source}
        (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def _prebuilt_usable(pre, d, speed):
        meta_path = pre / 'meta.json'
        if not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if meta.get('format') != PREBUILT_FORMAT or meta.get('speed', 'posted') != speed:
            return False
        src = _source_signature(d)
        return not src or src == meta.get('source')

    def _load_prebuilt(self, pre):
        meta = json.loads((pre / 'meta.json').read_text(encoding='utf-8'))
        a = {name: np.load(pre / f'{name}.npy', mmap_mode='r' if name in MMAP_ARRAYS else None)
             for name in PREBUILT_ARRAYS}
        self.n_links, self.n_nodes, self._source = meta['n_links'], meta['n_nodes'], meta['source']
        self._indices, self._indptr, self._starts, self._entry_link = (
            a['indices'], a['indptr'], a['starts'], a['entry_link'])
        self._w = {('time', True): a['w_time_exp'], ('time', False): a['w_time_noexp'],
                   ('dist', True): a['w_dist_exp'], ('dist', False): a['w_dist_noexp']}
        self.i1, self.i2, self.fwd, self.bwd = a['i1'], a['i2'], a['fwd'], a['bwd']
        self.is_expressway, self._mask_trunk, self._mask_major = a['is_expressway'], a['mask_trunk'], a['mask_major']
        self.link_dist, self.link_time_fwd, self.link_time_bwd = a['link_dist'], a['link_time_fwd'], a['link_time_bwd']
        self.ptr1, self.by2, self.ptr2 = a['ptr1'], a['by2'], a['ptr2']
        self.weak, self.strong = a['weak'], a['strong']
        self.kd_idx, self.kd_lat, self.kd_lon = a['kd_idx'], a['kd_lat'], a['kd_lon']
        self.coords, self.coord_offsets = a['coords'], a['coord_offsets']

    # ------------------------------------------------------------------ 共通の仕上げ
    def _finish(self):
        shape = (self.n_nodes, self.n_nodes)
        self.graphs = {k: csr_matrix((w, self._indices, self._indptr), shape=shape) for k, w in self._w.items()}
        self.class_masks = {'trunk': self._mask_trunk, 'major': self._mask_major}
        sizes = np.bincount(self.weak)
        self.n_components = len(sizes)
        self._weak_sizes = sizes
        self._weak_rank = np.empty(len(sizes), dtype=np.int64)
        self._weak_rank[np.argsort(sizes)[::-1]] = np.arange(1, len(sizes) + 1)
        self.kd = KDTree(_xyz(self.kd_lat, self.kd_lon))
        self.n_snap_nodes = len(self.kd_idx)

    # ------------------------------------------------------------------ 探索
    def snap(self, lat, lon):
        """最寄りの道路ノード（強連結成分 1,000 ノード以上）。(グラフ内index, 緯度, 経度, 距離m)"""
        chord, k = self.kd.query(_xyz([lat], [lon])[0])
        return (int(self.kd_idx[k]), float(self.kd_lat[k]), float(self.kd_lon[k]),
                float(2 * EARTH_R * math.asin(min(chord / 2, 1.0))))

    def component_name(self, idx):
        c = int(self.weak[idx])
        return f'{self._weak_rank[c]} 番目に大きい道路網・{self._weak_sizes[c]:,} ノード'

    def reachability(self, lat, lon, limit, cost='time', use_expressway=True, road_class='auto'):
        """到達圏。limit は分（cost=dist ならメートル）。返り値は KSJ 版と同じ形（link_ids 昇順・costs）"""
        t0 = time.perf_counter()
        idx, slat, slon, sm = self.snap(lat, lon)
        d = dijkstra(self.graphs[(cost, use_expressway)], directed=True, indices=idx, limit=limit)
        reached = np.flatnonzero(np.isfinite(d))
        t_dijkstra = time.perf_counter() - t0
        # 到達したノードから出るリンク: node1 側から順方向、node2 側から逆方向
        cand = np.union1d(_ranges(self.ptr1, reached), self.by2[_ranges(self.ptr2, reached)])
        c1 = np.where(self.fwd[cand], d[self.i1[cand]], np.inf)
        c2 = np.where(self.bwd[cand], d[self.i2[cand]], np.inf)
        c = np.minimum(c1, c2)
        ok = np.isfinite(c)
        if not use_expressway:
            ok &= ~self.is_expressway[cand]
        cand, c = cand[ok], c[ok]

        counts = {'all': len(cand)}
        counts.update({k: int(np.count_nonzero(m[cand])) for k, m in self.class_masks.items()})
        if road_class == 'auto':
            applied = next((k for k in ROAD_CLASS_ORDER if counts[k] <= LINKS_HARD_MAX), 'major')
        else:
            applied = road_class
            if counts[applied] > LINKS_HARD_MAX:
                raise TooManyLinks(applied, counts)
        if applied != 'all':
            keep = self.class_masks[applied][cand]
            cand, c = cand[keep], c[keep]
        return {
            'link_ids': cand.astype(np.int64),
            'costs': c,
            'snap': (slat, slon, sm),
            'road_class_applied': applied,
            'tier': 'ok' if len(cand) <= TIER_OK_MAX else 'heavy',
            'counts': counts,
            'nodes_reached': int(len(reached)),
            'dijkstra_ms': t_dijkstra * 1000,
            'elapsed_ms': (time.perf_counter() - t0) * 1000,
        }

    def _links_for_path(self, path, cost, use_expressway):
        """ノード列 → リンク列。辺 (u, v) の元のリンクのうち、重みが最小のもの（CSR に載っている重みのリンク）"""
        out = np.empty(len(path) - 1, dtype=np.int64)
        for n in range(len(path) - 1):
            u, v = path[n], path[n + 1]
            lo, hi = self._indptr[u], self._indptr[u + 1]
            j = lo + int(np.searchsorted(self._indices[lo:hi], v))
            end = self._starts[j + 1] if j + 1 < len(self._starts) else len(self._entry_link)
            cands = self._entry_link[self._starts[j]:end]
            if cost == 'time':
                w = np.where(self.i1[cands] == u, self.link_time_fwd[cands], self.link_time_bwd[cands])
            else:
                w = self.link_dist[cands].copy()
            if not use_expressway:
                w = np.where(self.is_expressway[cands], np.inf, w)
            out[n] = cands[np.argmin(w)]
        return out

    def route(self, from_lat, from_lon, to_lat, to_lon, cost='time', use_expressway=True):
        """2 地点間の最短経路。返り値は KSJ 版と同じ形"""
        t0 = time.perf_counter()
        s, slat, slon, sm = self.snap(from_lat, from_lon)
        t, tlat, tlon, tm = self.snap(to_lat, to_lon)
        base = {'snap': {'origin_m': sm, 'dest_m': tm}, 'link_ids': np.empty(0, dtype=np.int64),
                'coordinates': None, 'summary': {'dist_m': 0, 'time_min': 0.0, 'link_count': 0}}

        def fail(reason):
            base.update(unreachable_reason=reason, elapsed_ms=(time.perf_counter() - t0) * 1000)
            return base

        if self.weak[s] != self.weak[t]:
            return fail(f'始点と終点が別の道路網に属しています（{self.component_name(s)} / {self.component_name(t)}）')
        G = self.graphs[(cost, use_expressway)]
        # 直線距離（大円）から探索の上限を見積もり、届かなければ倍にして探し直す（KSJ 版と同じ）
        km = self._great_circle_m(slat, slon, tlat, tlon) / 1000
        guess = (km * ROUTE_MIN_PER_KM[use_expressway] + 15) if cost == 'time' else (km * 1500 + 2000)
        tries = 0
        for limit in [guess * 2 ** k for k in range(ROUTE_LIMIT_TRIES)] + [np.inf]:
            tries += 1
            d, pred = dijkstra(G, directed=True, indices=s, return_predecessors=True, limit=limit)
            if np.isfinite(d[t]):
                break
        if not np.isfinite(d[t]):
            return fail('高速道路を使わないと到達できません' if not use_expressway else '経路が見つかりません')

        path = [t]
        while path[-1] != s:
            path.append(pred[path[-1]])
        path.reverse()
        link_ids = self._links_for_path(path, cost, use_expressway)

        parts = []
        for n, lid in enumerate(link_ids):
            xy = np.asarray(self.coords[self.coord_offsets[lid]:self.coord_offsets[lid + 1]])
            if self.i1[lid] != path[n]:
                xy = xy[::-1]
            parts.append(xy if n == 0 else xy[1:])
        coords = np.concatenate(parts) if parts else np.empty((0, 2), dtype=np.float32)

        fwd = self.i1[link_ids] == np.asarray(path[:-1])
        time_min = np.where(fwd, self.link_time_fwd[link_ids], self.link_time_bwd[link_ids]).sum()
        base.update(
            coordinates=coords, link_ids=link_ids, unreachable_reason=None,
            summary={'dist_m': int(round(self.link_dist[link_ids].sum())),
                     'time_min': round(float(time_min), 1),
                     'link_count': int(len(link_ids)),
                     'nodes_searched': int(np.isfinite(d).sum()), 'tries': tries},
            elapsed_ms=(time.perf_counter() - t0) * 1000)
        return base

    @staticmethod
    def _great_circle_m(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
        return 2 * EARTH_R * math.asin(math.sqrt(h))

    def health(self):
        return {
            'case': self.case, 'speed': self.speed, 'links': self.n_links, 'nodes': self.n_nodes, 'snap_nodes': self.n_snap_nodes,
            'components': int(self.n_components), 'coordinates': int(len(self.coords)),
            'load_seconds': round(self.load_seconds, 2), 'graph_source': self.source,
        }
