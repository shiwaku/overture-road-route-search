"""main.py が使うもの: グラフの状態・到達圏レスポンスの組み立て（../ksj-route-search-api の api/core.py から移植）

KSJ 版からの変更: 日本の範囲のチェックを外した。上限は全球向けに広げた。ブックマーク（DB）は持たない
"""

import os
import struct
import threading
import time

import numpy as np
import orjson
from fastapi import HTTPException
from fastapi.responses import Response

from api.graph import RoadGraph, TooManyLinks

CASE = os.environ.get('ROUTE_CASE', 'jp')        # network/<case> を読む（jp・world）
LIMIT_MIN_MAX = 6000         # 分。全球では大陸を横断できるだけの上限（KSJ 版は 1,920）
LIMIT_DIST_MAX = 5_000_000   # cost=dist のときの上限（m）
REACH_CONCURRENCY = 2
_reach_slots = threading.BoundedSemaphore(REACH_CONCURRENCY)

state = {'graph': None, 'started_at': time.time(), 'error': None}


def graph() -> RoadGraph:
    g = state['graph']
    if g is None:
        if state['error']:
            raise HTTPException(500, f'グラフの読み込みに失敗しました: {state["error"]}')
        raise HTTPException(503, 'グラフを読み込み中です')
    return g


def check_latlon(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, '緯度経度の範囲外です')


# format=bin のコストの単位（16 ビット整数に丸める）。時間は 0.1 分（上限 6,000 分 → 60,000）、距離は 100 m（上限 5,000 km → 50,000）
BIN_COST_UNIT = {'time': 0.1, 'dist': 100.0}


def _binary_body(meta, link_ids, costs, cost):
    """到達圏のバイナリ形式（KSJ 版と同じ）。
    [u32 ヘッダ長 H][ヘッダ JSON][link_id の差分 u32 × count][コスト u16 × count]（リトルエンディアン）"""
    unit = BIN_COST_UNIT[cost]
    head = orjson.dumps({**meta, 'cost_unit': unit, 'encoding': 'delta-u32+u16'}, option=orjson.OPT_SERIALIZE_NUMPY)
    head += b' ' * (-(4 + len(head)) % 4)
    deltas = np.diff(link_ids, prepend=0).astype('<u4')
    q = np.minimum(np.round(costs / unit), 65535).astype('<u2')
    return struct.pack('<I', len(head)) + head + deltas.tobytes() + q.tobytes()


def reachability_response(lat, lon, limit_min, cost='time', road_class='auto', use_expressway=True, fmt='json'):
    check_latlon(lat, lon)
    hard = LIMIT_MIN_MAX if cost == 'time' else LIMIT_DIST_MAX
    if limit_min > hard:
        raise HTTPException(422, f'limit_min は {hard:,} 以下にしてください（cost={cost}）')
    g = graph()
    with _reach_slots:
        try:
            r = g.reachability(lat, lon, limit_min, cost=cost, use_expressway=use_expressway, road_class=road_class)
        except TooManyLinks as e:
            raise HTTPException(400, str(e))
        slat, slon, sm = r['snap']
        meta = {
            'origin': {'lat': lat, 'lon': lon},
            'snap': {'lat': slat, 'lon': slon, 'dist_m': round(sm, 1)},
            'limit_min': limit_min, 'cost': cost,
            'road_class': road_class, 'use_expressway': use_expressway,
            'road_class_applied': r['road_class_applied'], 'tier': r['tier'],
            'counts': r['counts'], 'count': int(len(r['link_ids'])),
            'nodes_reached': r['nodes_reached'],
            'dijkstra_ms': round(r['dijkstra_ms'], 1),
            'elapsed_ms': round(r['elapsed_ms'], 1),
        }
        if fmt == 'bin':
            return Response(_binary_body(meta, r['link_ids'], r['costs'], cost), media_type='application/octet-stream')
        body = {**meta, 'link_ids': r['link_ids'], 'costs': np.round(r['costs'].astype(np.float64), 2)}
        return Response(orjson.dumps(body, option=orjson.OPT_SERIALIZE_NUMPY), media_type='application/json')
