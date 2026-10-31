"""制限速度が無いリンクに、国ごとの法定速度（既定の制限速度）を当てる（issue #4 の案 B）

制限速度の入り方は国でまちまちで（北京はほぼ 0%、ロサンゼルスの motorway は 94%）、無い区間を class の既定値で
埋めると、データの埋まり具合が都市の比較に混ざる。そこで無い区間は、その国（州）の法律の既定の速度で埋める。

法定速度の表: osm-legal-default-speeds（https://github.com/westnordost/osm-legal-default-speeds）の
legal_default_speeds.json。元は OSM wiki「Default speed limits」（CC BY-SA 2.0）。data/legal_speeds/ に置く。
道路の種類の選び方はライブラリと同じ（LegalDefaultSpeeds.getSpeedLimits）:
  1. 国（州）の一覧で、名前の無い行（既定）より下の行を下から、上の行を上から見て、最初に当てはまる行
     （まずタグの完全一致 filter だけ、次に fuzzyFilter も使う）
  2. どれにも当たらなければ名前の無い行
OSM のタグは Overture から作る（車線数・中央分離帯は Overture にほぼ無いので仮定を置く）:
  highway = class（ランプは <class>_link）、oneway = yes（一方通行）
  dual_carriageway = yes: 一方通行の本線で、motorway・trunk か郊外のもの（市街地の一方通行は一方通行の街路とみなす）
  lanes = 2: 一方通行の motorway・trunk の本線（片側 2 車線とみなす。lanes が無いと表では「片側 1 車線」になる）
{urban}・{rural} はタグからは分からないので、下の「市街地」で置き換える。ただし motorway は市街地でも郊外として引く
（米国の州などの表には motorway の行が無く、市街地の行 25 mph が当たってしまうため）。

市街地: 幹線（このネットワーク）の密度で推定する。リンクの始点を 1 km 四方のマスに入れ、周り 3×3 マスの
道路延長 ÷ 9 km² が URBAN_KM_PER_KM2 以上なら市街地。幹線だけなので粗い推定。

国: Overture の divisions（division_area）の country の範囲（領海を含む maritime を優先）にノードの位置を入れる。
州で法定速度が違う国（US・CA・AU など）は region の範囲も使う。

出力: network/<case>/legal_speeds.parquet（link_id の順。key・urban・legal_type・legal_kmh）
探索グラフは速度 legal（api/graph.py）で使う: 制限速度があればそれ、無ければ legal_kmh、それも無ければ class の既定値

    uv run python scripts/14_legal_speeds.py --case world
    uv run python scripts/14_legal_speeds.py --case world --probe     # 都市の密度を見てしきい値を決める
"""

import argparse
import json
import math
import re
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
LEGAL_JSON = BASE / 'data' / 'legal_speeds' / 'legal_default_speeds.json'
DIVISIONS = BASE / 'data' / 'world' / RELEASE / 'divisions_area.parquet'
S3 = f's3://overturemaps-us-west-2/release/{RELEASE}/theme=divisions/type=division_area/*'
CELL_KM = 1.0
URBAN_KM_PER_KM2 = 3.0   # 都市の中心は 5〜16、郊外は 0〜2.8（--probe で見た値）
MPH = 1.609344

ap = argparse.ArgumentParser()
ap.add_argument('--case', default='world')
ap.add_argument('--urban', type=float, default=URBAN_KM_PER_KM2, help='市街地とみなす幹線の密度（km/km²）')
ap.add_argument('--probe', action='store_true', help='都市と郊外の点の密度を出すだけ')
a = ap.parse_args()
NET = BASE / 'network' / a.case
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


# ------------------------------------------------------------------ フィルタ式（osm-legal-default-speeds の書き方）
TOKEN = re.compile(r'\s*(\(|\)|\{[^}]*\}|!~|!=|>=|<=|~|=|>|<|!|"(?:[^"\\]|\\.)*"|[^\s()=~!<>"]+)')


def tokenize(s):
    out, pos = [], 0
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m:
            if s[pos:].strip() == '':
                break
            raise ValueError(f'読めないフィルタ: {s[pos:]!r}')
        out.append(m.group(1))
        pos = m.end()
    return out


def parse(s):
    """→ 入れ子のタプル。('or', [..]) ('and', [..]) ('ph', 名前) ('has', k) ('not', k) ('cmp', k, op, v)"""
    toks = tokenize(s)
    i = 0

    def value(t):
        return t[1:-1].replace('\\"', '"') if t.startswith('"') else t

    def expr():
        nonlocal i
        terms = [term()]
        while i < len(toks) and toks[i] == 'or':
            i += 1
            terms.append(term())
        return terms[0] if len(terms) == 1 else ('or', terms)

    def term():
        nonlocal i
        fs = [factor()]
        while i < len(toks) and toks[i] == 'and':
            i += 1
            fs.append(factor())
        return fs[0] if len(fs) == 1 else ('and', fs)

    def factor():
        nonlocal i
        t = toks[i]
        i += 1
        if t == '(':
            e = expr()
            assert toks[i] == ')'
            i += 1
            return e
        if t.startswith('{'):
            return ('ph', t[1:-1])
        if t == '!':
            if toks[i] == '(' or toks[i].startswith('{'):
                return ('neg', factor())                    # !(…)・!{名前}
            k = toks[i]
            i += 1
            return ('not', k)
        if i < len(toks) and toks[i] in ('~', '!~', '=', '!=', '>=', '<=', '>', '<'):
            op, v = toks[i], value(toks[i + 1])
            i += 2
            return ('cmp', t, op, re.compile(v) if op in ('~', '!~') else v)
        return ('has', t)

    e = expr()
    assert i == len(toks), f'余り: {toks[i:]}'
    return e


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ev(node, tags, ph):
    kind = node[0]
    if kind == 'or':
        return any(ev(n, tags, ph) for n in node[1])
    if kind == 'and':
        return all(ev(n, tags, ph) for n in node[1])
    if kind == 'ph':
        return ph(node[1])
    if kind == 'neg':
        return not ev(node[1], tags, ph)
    if kind == 'has':
        return node[1] in tags
    if kind == 'not':
        return node[1] not in tags
    _, k, op, v = node
    x = tags.get(k)
    if op == '~':
        return x is not None and v.fullmatch(x) is not None
    if op == '!~':
        return x is None or v.fullmatch(x) is None
    if op == '=':
        return x == v
    if op == '!=':
        return x != v
    a_, b_ = num(x), num(v)
    if a_ is None or b_ is None:
        return False
    return {'>=': a_ >= b_, '<=': a_ <= b_, '>': a_ > b_, '<': a_ < b_}[op]


class Legal:
    def __init__(self, path):
        j = json.loads(Path(path).read_text(encoding='utf-8'))
        self.meta = j['meta']
        self.types = {k: {f: parse(v[f]) for f in ('filter', 'fuzzyFilter') if f in v}
                      for k, v in j['roadTypesByName'].items()}
        self.by_code = j['speedLimitsByCountryCode']

    def key(self, country, region):
        if region in self.by_code:
            return region
        return country if country in self.by_code else None

    def lookup(self, key, tags, urban):
        """→ (道路の種類の名前 or None, km/h or None)"""
        rows = self.by_code[key]
        override = {'urban': urban, 'rural': not urban}

        def match(name, fuzzy):
            if name in override:
                return override[name]
            f = self.types.get(name)
            if f is None:
                return False
            ph = lambda n: match(n, fuzzy)  # noqa: E731
            return ('filter' in f and ev(f['filter'], tags, ph)) or \
                (fuzzy and 'fuzzyFilter' in f and ev(f['fuzzyFilter'], tags, ph))

        for fuzzy in (False, True):
            for seq in (reversed(rows), rows):
                for r in seq:
                    if r.get('name') is None:
                        break
                    if match(r['name'], fuzzy):
                        return r['name'], self.speed(r['tags'])
        fb = next((r for r in rows if r.get('name') is None), None)
        return (None, self.speed(fb['tags'])) if fb else (None, None)

    @staticmethod
    def speed(tags):
        """maxspeed → km/h。none（ドイツのアウトバーンなど）は maxspeed:advisory"""
        v = tags.get('maxspeed')
        if v in (None, 'none'):
            v = tags.get('maxspeed:advisory')
        if v is None:
            return None
        if v == 'walk':
            return 6.0
        m = re.fullmatch(r'\s*([\d.]+)\s*(mph)?\s*', v)
        if not m:
            return None
        return float(m.group(1)) * (MPH if m.group(2) else 1.0)


legal = Legal(LEGAL_JSON)


def osm_tags(highway, oneway, urban):
    tags = {'highway': highway}
    if oneway:
        tags['oneway'] = 'yes'
        main = not highway.endswith('_link')
        if main and (highway in ('motorway', 'trunk') or not urban):
            tags['dual_carriageway'] = 'yes'
        else:
            tags['dual_carriageway'] = 'no'    # 表の fuzzyFilter は一方通行を中央分離帯ありとみなすので打ち消す
        if main and highway in ('motorway', 'trunk'):
            tags['lanes'] = '2'
    return tags


# ------------------------------------------------------------------ 市街地（幹線の密度）
el('リンクとノードを読む')
links = pq.read_table(NET / 'links.parquet', columns=['node1', 'class', 'is_link', 'oneway', 'speed_posted', 'dist_m'])
nodes = pq.read_table(NET / 'nodes.parquet', columns=['lon', 'lat'])
lon, lat = nodes['lon'].to_numpy(), nodes['lat'].to_numpy()
n1 = links['node1'].to_numpy()
dist = links['dist_m'].to_numpy()


def cell_of(lo, la):
    y = np.floor(la * 110.57 / CELL_KM).astype(np.int64)
    x = np.floor(lo * 111.32 * np.cos(np.radians(la)) / CELL_KM).astype(np.int64)
    return x, y


el('幹線の密度（1 km マス・周り 3×3）')
lx, ly = cell_of(lon[n1], lat[n1])
KEY = 1 << 21                                   # x は ±2 万 km 程度なので 2^21 で足りる
ck = (ly + KEY) * (2 * KEY) + (lx + KEY)
uk, inv = np.unique(ck, return_inverse=True)
km = np.bincount(inv, weights=dist / 1000)
dens = np.zeros(len(uk))
for dy in (-1, 0, 1):
    for dx in (-1, 0, 1):
        nb = uk + dy * (2 * KEY) + dx
        j = np.searchsorted(uk, nb)
        j[j == len(uk)] = 0
        hit = uk[j] == nb
        dens[hit] += km[j[hit]]
dens /= 9 * CELL_KM ** 2
link_dens = dens[inv]
del lx, ly, ck

if a.probe:
    P = {'東京 都心': (35.68, 139.77), '東京 八王子': (35.66, 139.32), '長野 郊外': (36.30, 138.10),
         'パリ 中心': (48.86, 2.35), 'パリ 70 km 南': (48.30, 2.40), 'ロサンゼルス': (34.05, -118.24),
         'カンザス 農村': (38.50, -98.50), 'デリー 中心': (28.61, 77.21), 'デリー 80 km 東': (28.60, 78.00),
         '北京 中心': (39.90, 116.40), '北京 90 km 南': (39.10, 116.40), 'ロンドン': (51.51, -0.13)}
    for name, (la, lo) in P.items():
        x, y = cell_of(np.array([lo]), np.array([la]))
        k = (y + KEY) * (2 * KEY) + (x + KEY)
        j = np.searchsorted(uk, k)[0]
        print(f'  {name}: {dens[j] if j < len(uk) and uk[j] == k[0] else 0:.2f} km/km²')
    q = np.percentile(link_dens, [10, 25, 50, 75, 90])
    print('  リンクの密度の分位（10/25/50/75/90%）:', ' / '.join(f'{v:.2f}' for v in q))
    for t in (1, 1.5, 2, 3, 4):
        print(f'  しきい値 {t}: 市街地のリンク {np.mean(link_dens >= t):.1%}')
    raise SystemExit

urban = link_dens >= a.urban
el(f'  市街地のリンク {urban.mean():.1%}（{a.urban} km/km² 以上）')

# ------------------------------------------------------------------ 国・州
if not DIVISIONS.exists():
    el('divisions の国・州の範囲を S3 から取る')
    sub = sorted({k.split('-')[0] for k in legal.by_code if '-' in k})
    c = duckdb.connect()
    c.sql("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2'")
    tmp = DIVISIONS.with_suffix('.tmp')
    c.sql(f"""COPY (
        SELECT subtype, country, region, class, ST_SimplifyPreserveTopology(geometry, 0.001) AS geometry
        FROM read_parquet('{S3}')
        WHERE subtype = 'country' OR (subtype = 'region' AND country IN {tuple(sub)})
    ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)""")
    tmp.rename(DIVISIONS)

div = pq.read_table(DIVISIONS).to_pandas()
div['geometry'] = shapely.from_wkb(div['geometry'])


def areas(subtype):
    """領海を含む maritime を優先し、maritime が無いものは land（海の上の橋を落とさない）"""
    col = 'country' if subtype == 'country' else 'region'
    d = div[(div.subtype == subtype) & div[col].notna()]
    has_mar = set(d.loc[d['class'] == 'maritime', col])
    d = d[(d['class'] == 'maritime') | ~d[col].isin(has_mar)]
    return d[col].to_numpy(), d.geometry.to_numpy()


def tiles(geoms, step=1.0):
    """大きなポリゴン（海岸線が細かい国）を step 度四方に切る → (切れ端, 元の添字)。点の判定が速くなる"""
    pieces, owner = [], []
    for i, g in enumerate(geoms):
        x0, y0, x1, y1 = g.bounds
        xs = np.arange(math.floor(x0 / step) * step, x1, step)
        ys = np.arange(math.floor(y0 / step) * step, y1, step)
        if len(xs) * len(ys) <= 1:
            pieces.append(g)
            owner.append(i)
            continue
        gx, gy = np.meshgrid(xs, ys)
        boxes = shapely.box(gx.ravel(), gy.ravel(), gx.ravel() + step, gy.ravel() + step)
        shapely.prepare(g)
        boxes = boxes[shapely.intersects(g, boxes)]
        cut = shapely.intersection(g, boxes)
        cut = cut[~shapely.is_empty(cut)]
        pieces.extend(cut)
        owner.extend([i] * len(cut))
    return np.array(pieces, dtype=object), np.array(owner)


def locate(codes, geoms, idx, chunk=2_000_000):
    """ノード idx の位置 → codes の添字（どこにも入らなければ -1）"""
    geoms, owner = tiles(geoms)
    el(f'    範囲を {len(geoms):,} 片に切った')
    tree = shapely.STRtree(geoms)
    out = np.full(len(idx), -1, dtype=np.int32)
    for s in range(0, len(idx), chunk):
        sl = idx[s:s + chunk]
        pts = shapely.points(lon[sl], lat[sl])
        p, g = tree.query(pts, predicate='intersects')
        first = np.unique(p, return_index=True)[1]          # 重なり（係争地）は最初の 1 つ
        out[s + p[first]] = owner[g[first]]
        el(f'    {min(s + chunk, len(idx)):,} / {len(idx):,}')
    return out


el('ノードの国')
uniq_n1 = np.unique(n1)                                     # リンクの始点になるノードだけ
c_codes, c_geoms = areas('country')
ci = locate(c_codes, c_geoms, uniq_n1)
node_country = np.full(len(lon), '', dtype=object)
node_country[uniq_n1[ci >= 0]] = c_codes[ci[ci >= 0]]
el(f'  国が決まらないノード {np.mean(ci < 0):.2%}')

el('州で法定速度が違う国のノードの州')
r_codes, r_geoms = areas('region')
need = uniq_n1[np.isin(node_country[uniq_n1], np.unique([cc.split('-')[0] for cc in r_codes]))]
ri = locate(r_codes, r_geoms, need)
node_region = np.full(len(lon), '', dtype=object)
node_region[need[ri >= 0]] = r_codes[ri[ri >= 0]]

# ------------------------------------------------------------------ 法定速度（組み合わせごとに 1 回だけ引く）
el('法定速度を当てる')
cls = links['class'].to_numpy(zero_copy_only=False)
is_link = links['is_link'].to_numpy(zero_copy_only=False)
oneway = links['oneway'].to_numpy(zero_copy_only=False)
country = node_country[n1]
region = node_region[n1]
# (国, 州) の組ごとに表の行の名前（州 → 国の順に探す）を決め、リンクに配る
pairs, pinv = np.unique(np.char.add(np.char.add(country.astype(str), '|'), region.astype(str)), return_inverse=True)
pair_key = np.array([legal.key(*p.split('|')) or '' for p in pairs])
link_key = pair_key[pinv]
combo = np.char.add(np.char.add(np.char.add(link_key, '|'), np.char.add(cls.astype(str), np.where(is_link, '_link', ''))),
                    np.char.add(np.where(oneway, '|1', '|0'), np.where(urban, '|u', '|r')))
cu, cinv = np.unique(combo, return_inverse=True)
c_type, c_kmh = [], []
for s in cu:
    k, hw, ow, ur = s.split('|')
    if not k:
        c_type.append('')
        c_kmh.append(np.nan)
        continue
    u = ur == 'u' and not hw.startswith('motorway')
    t, v = legal.lookup(k, osm_tags(hw, ow == '1', u), u)
    c_type.append(t or '(既定)')
    c_kmh.append(np.nan if v is None else v)
legal_type = np.array(c_type, dtype=object)[cinv]
legal_kmh = np.array(c_kmh, dtype=np.float32)[cinv]
el(f'  組み合わせ {len(cu):,}・法定速度が引けたリンク {np.isfinite(legal_kmh).mean():.1%}')

pq.write_table(pa.table({
    'country': country.astype(str), 'key': link_key, 'urban': urban, 'legal_type': legal_type.astype(str), 'legal_kmh': legal_kmh,
}), NET / 'legal_speeds.parquet', compression='zstd')
(NET / 'legal_speeds.json').write_text(json.dumps({
    'source': legal.meta, 'urban_km_per_km2': a.urban, 'cell_km': CELL_KM}, ensure_ascii=False, indent=2), encoding='utf-8')

posted = links['speed_posted'].to_numpy(zero_copy_only=False)
miss = ~posted
print(f'制限速度の無いリンク {miss.mean():.1%} のうち、法定速度で埋まる {np.isfinite(legal_kmh[miss]).mean():.1%}')
el(f'書き出し: {NET / "legal_speeds.parquet"}')
