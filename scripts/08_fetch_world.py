"""全球の幹線の segment を S3 から取得する（PLAN.md 段階 3）

S3 の segment は 128 ファイル・約 64 GB。幹線（motorway〜tertiary・ランプを含む）だけを取り出し、
入力ファイル 1 つにつき出力ファイル 1 つで data/world/<版>/segment/ に保存する。
取得済みのファイルは飛ばすので、途中で止まっても同じコマンドで続きから取れる。

connector は取らない。ノードの位置は segment の connectors[].at（線上の位置の割合）から線形補間で求める（段階 4）。

    uv run python scripts/08_fetch_world.py              # 全部
    uv run python scripts/08_fetch_world.py --limit 1    # 最初の 1 ファイルだけ（時間を見積もる）

DuckDB の既定はメモリの 8 割まで使うので、他の処理と並べるとメモリ不足になる。--memory と --threads で抑える。
"""

import argparse
import time
from pathlib import Path

import duckdb

RELEASE = '2026-10-21.0'
S3 = f's3://overturemaps-us-west-2/release/{RELEASE}/theme=transportation/type=segment'
CLASSES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / 'data' / 'world' / RELEASE / 'segment'

ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, help='最初の N ファイルだけ取る')
ap.add_argument('--memory', default='4GB', help='DuckDB のメモリ上限')
ap.add_argument('--threads', type=int, default=4, help='DuckDB のスレッド数')
a = ap.parse_args()
OUT.mkdir(parents=True, exist_ok=True)

c = duckdb.connect()
c.sql("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
# 上限を超えた分は一時ファイルに逃がす。行の順序は保たなくてよいので、そのぶんメモリを減らす
c.sql(f"SET memory_limit='{a.memory}'; SET threads={a.threads}; SET preserve_insertion_order=false; "
      f"SET temp_directory='{(OUT.parent / 'duckdb_tmp').as_posix()}'")
t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.0f}s] {msg}', flush=True)


files = [r[0] for r in c.sql(f"SELECT file FROM glob('{S3}/*') ORDER BY file").fetchall()]
el(f'{len(files)} ファイル')
for i, f in enumerate(files[:a.limit]):
    p = OUT / f'{i:03d}.parquet'
    if p.exists():
        continue
    # sources・destinations は使わないので落とす。途中で落ちると壊れたファイルが残るので一時名で書く
    tmp = p.with_suffix('.tmp')
    c.sql(f"""COPY (
        SELECT * EXCLUDE (sources, destinations) FROM read_parquet('{f}')
        WHERE subtype = 'road' AND class IN {CLASSES}
    ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)""")
    tmp.rename(p)
    n = c.sql(f"SELECT count(*) FROM read_parquet('{p}')").fetchall()[0][0]
    el(f'  {i + 1:3d}/{len(files)}  {n:,} 本  {p.stat().st_size / 1e6:.0f} MB')

el('完了')
