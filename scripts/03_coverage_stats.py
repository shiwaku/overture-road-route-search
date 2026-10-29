"""Overture の道路延長を道路統計年報（国交省）と比べる（PLAN.md 段階 2）

道路統計年報 2025（2024-03-31 時点）の実延長と、Overture の延長を、都道府県別に比べる。
- 年報の表の単位は m（見出しの「Km」は誤り）
- 年報の都道府県の行は政令指定都市を含まない。市の行（4 桁の番号の先頭 2 桁が都道府県）を足し戻す

道路の種類は 2 つにまとめて比べる:
- 高速＋一般国道: Overture の motorway（自動車専用道路。都市高速も入る）と、OSM の路線 JP:E / JP:national を持つ道
- 都道府県道＋市町村道: それ以外の車道（trunk〜tertiary・residential・unclassified・living_street）
都道府県道だけで分けないのは、OSM で都道府県道の路線（routes）が入っていない県が多いため（青森 12%・山形 1% など）

Overture 側の数え方:
- 上下線が別の線になっている道は 2 本で数えると統計の倍になる。
  一方通行の segment のうち、幹線（motorway〜tertiary）か路線（routes）を持つものは 0.5 倍で数える。
  生活道路の一方通行は 1 本の道なのでそのまま数える
- ランプ・連絡路（subclass=link）は外す（統計は本線の延長）
- 係争地（country が JP 以外）は外す

    uv run python scripts/03_coverage_stats.py
出力: output/coverage/stats_by_group.csv, stats_by_pref.csv, other_roads.csv, fill_rate.csv
"""

from pathlib import Path

import duckdb
import openpyxl
import pandas as pd

RELEASE = '2026-10-21.0'
BASE = Path(__file__).resolve().parent.parent
JP = BASE / 'data' / 'jp' / RELEASE
STATS = BASE / 'data' / 'stats' / 'road_nenpo_2025'
OUT = BASE / 'output' / 'coverage'
OUT.mkdir(parents=True, exist_ok=True)

TABLES = {'expressway': '05', 'national': '08', 'prefectural': '11', 'municipal': '14'}
GROUP = {'expressway': 'major', 'national': 'major', 'prefectural': 'local', 'municipal': 'local'}
LABEL = {'major': '高速＋一般国道', 'local': '都道府県道＋市町村道', 'total': '合計'}
NATIONAL_TOTAL_KM = 1_231_333       # 表2 の実延長の合計（読み取りの確かめ用）
CAR = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'living_street')
TRUNKISH = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')


def read_stats():
    """表5・8・11・14 の都道府県と政令指定都市の行の実延長（9 列目）を、都道府県ごと・種類ごとに km で返す"""
    rows = []
    for typ, no in TABLES.items():
        ws = openpyxl.load_workbook(STATS / f'd_genkyou{no}.xlsx', read_only=True, data_only=True).worksheets[0]
        for r in ws.iter_rows(values_only=True):
            name = str(r[0] or '').replace(' ', '')
            code = name[:len(name) - len(name.lstrip('0123456789'))]
            if len(code) in (2, 4) and 1 <= int(code[:2]) <= 47 and isinstance(r[8], (int, float)):
                rows.append({'pref': f'JP-{code[:2]}', 'name': name[len(code):], 'is_city': len(code) == 4,
                             'type': typ, 'stats_km': r[8] / 1000})
    df = pd.DataFrame(rows)
    names = df[~df.is_city].drop_duplicates('pref').set_index('pref')['name']
    df = df.groupby(['pref', 'type'], as_index=False)['stats_km'].sum()
    df['pref_name'] = df.pref.map(names)
    df['group'] = df.type.map(GROUP)
    return df


stats = read_stats()
assert stats.pref.nunique() == 47
assert abs(stats.stats_km.sum() - NATIONAL_TOTAL_KM) < 2, stats.stats_km.sum()
stats_g = stats.groupby(['pref', 'pref_name', 'group'], as_index=False)['stats_km'].sum()

c = duckdb.connect()
c.sql(f"""CREATE VIEW r AS
    SELECT *,
        CASE WHEN oneway AND (class IN {TRUNKISH} OR network IS NOT NULL) THEN 0.5 ELSE 1.0 END * length_m / 1000 AS km_c,
        CASE
            WHEN class = 'motorway' OR network IN ('expressway', 'national') THEN 'major'
            WHEN class IN {CAR} THEN 'local'
        END AS "group"
    FROM read_parquet('{JP / 'segment_attrs.parquet'}')
    WHERE subtype = 'road' AND country = 'JP'""")

ov = c.sql("""SELECT pref, "group", sum(km_c) AS overture_km, sum(length_m) / 1000 AS overture_km_raw
    FROM r WHERE NOT is_link AND "group" IS NOT NULL GROUP BY ALL""").df()
by_pref = stats_g.merge(ov, on=['pref', 'group'], how='left')


def with_total(df, keys):
    t = df.groupby(keys, as_index=False)[['stats_km', 'overture_km', 'overture_km_raw']].sum().assign(group='total')
    out = pd.concat([df, t])
    out['ratio'] = (out.overture_km / out.stats_km).round(3)
    return out


# 全国
by_group = with_total(by_pref.groupby('group', as_index=False)[['stats_km', 'overture_km', 'overture_km_raw']].sum().assign(k=1), ['k'])
by_group['label'] = by_group.group.map(LABEL)
by_group = by_group.drop(columns='k').round(0)
by_group['ratio'] = (by_group.overture_km / by_group.stats_km).round(3)
by_group.to_csv(OUT / 'stats_by_group.csv', index=False, encoding='utf-8-sig')
print(by_group.to_string(index=False))
print(f"（都道府県が決まらない JP の車道: {c.sql('SELECT round(sum(km_c)) FROM r WHERE pref IS NULL AND NOT is_link AND \"group\" IS NOT NULL').fetchall()[0][0]} km）")

# 統計の対象外の道（車道以外）
other = c.sql("""SELECT class, round(sum(km_c)) AS km FROM r
    WHERE NOT is_link AND "group" IS NULL GROUP BY class ORDER BY km DESC""").df()
other.to_csv(OUT / 'other_roads.csv', index=False, encoding='utf-8-sig')
print('\n統計の対象外の道（車道以外・ランプを除く）:')
print(other.to_string(index=False))

# 都道府県別
by_pref = with_total(by_pref, ['pref', 'pref_name'])
by_pref.round(1).sort_values(['pref', 'group']).to_csv(OUT / 'stats_by_pref.csv', index=False, encoding='utf-8-sig')
wide = by_pref.pivot_table(index=['pref', 'pref_name'], columns='group', values='ratio').reset_index()
print('\n都道府県別の比（Overture / 統計）:')
print(wide[['pref', 'pref_name', 'major', 'local', 'total']].to_string(index=False))

# 属性の埋まり具合（延長で重みづけ）
cols = ['has_name', 'has_speed', 'oneway', 'has_width', 'has_surface', 'has_access', 'has_route']
fill = c.sql(f"""
    SELECT class, round(sum(length_m) / 1000) AS km,
        {', '.join(f'round(coalesce(sum(CASE WHEN {k} THEN length_m END), 0) / sum(length_m), 3) AS {k}' for k in cols)}
    FROM r GROUP BY class ORDER BY km DESC""").df()
fill.to_csv(OUT / 'fill_rate.csv', index=False, encoding='utf-8-sig')
print('\n属性の埋まり具合（延長に対する割合）:')
print(fill.to_string(index=False))
