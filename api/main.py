"""
Route Search API（FastAPI）— Overture の道路ネットワークでの到達圏・経路探索

../ksj-route-search-api の api/main.py から移植。エンドポイントの形は KSJ 版と同じ（/health /reachability /route）。
読むネットワークは環境変数 ROUTE_CASE（jp・world。既定は jp）。

  uv run uvicorn api.main:app --port 8000
  ROUTE_CASE=world uv run uvicorn api.main:app --port 8010
"""

import asyncio
import gc
import time
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from api.core import CASE, check_latlon, graph, reachability_response, state
from api.graph import RoadGraph


@asynccontextmanager
async def lifespan(app: FastAPI):
    # グラフの読み込みはスレッドで回し、完了までは探索系が 503 を返す
    loop = asyncio.get_running_loop()

    def load():
        try:
            state['graph'] = RoadGraph(CASE)
        except Exception as e:
            state['error'] = repr(e)
        gc.collect()
    loop.run_in_executor(None, load)
    yield


app = FastAPI(title='Overture Road Route Search API', version='0.1', lifespan=lifespan,
              description='Overture の道路ネットワークでの到達圏・経路探索')
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=1)
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173'],
                   allow_methods=['*'], allow_headers=['*'])


@app.get('/health')
def health():
    g = state['graph']
    body = {'status': 'ok' if g else ('error' if state['error'] else 'loading'),
            'graph_loaded': g is not None,
            'uptime_seconds': round(time.time() - state['started_at'], 1),
            'features': []}
    if g:
        body.update(g.health())
    if state['error']:
        body['error'] = state['error']
    return body


@app.get('/reachability')
def reachability(
    lat: float, lon: float,
    limit_min: float = Query(120, gt=0, description='打ち切り。分（cost=dist のときはメートル）'),
    cost: Literal['time', 'dist'] = 'time',
    road_class: Literal['auto', 'all', 'trunk', 'major'] = 'auto',
    use_expressway: bool = True,
    format: Literal['json', 'bin'] = Query('json', description='bin は配列をバイナリで返す（画面用。形式は api/core.py の _binary_body）'),
):
    """到達圏。ジオメトリは返さず link_id とコストの並列配列を返す（クライアントは PMTiles に setFeatureState）"""
    return reachability_response(lat, lon, limit_min, cost=cost, road_class=road_class, use_expressway=use_expressway,
                                 fmt=format)


@app.get('/route')
def route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    cost: Literal['time', 'dist'] = 'time',
    use_expressway: bool = True,
):
    """2 地点間の最短経路。経路だけは LineString を返す。到達不能は 200 で geometry=null"""
    check_latlon(from_lat, from_lon)
    check_latlon(to_lat, to_lon)
    r = graph().route(from_lat, from_lon, to_lat, to_lon, cost=cost, use_expressway=use_expressway)
    geom = None
    if r['coordinates'] is not None:
        geom = {'type': 'LineString',
                'coordinates': np.round(r['coordinates'].astype(np.float64), 5).tolist()}
    return {
        'geometry': geom,
        'summary': r['summary'],
        'snap': {k: round(v, 1) for k, v in r['snap'].items()},
        'cost': cost, 'use_expressway': use_expressway,
        'unreachable_reason': r['unreachable_reason'],
        'elapsed_ms': round(r['elapsed_ms'], 1),
    }
