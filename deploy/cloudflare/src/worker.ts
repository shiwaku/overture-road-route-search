// ブラウザ → この Worker → パスで振り分け（../ksj-route-search-api の deploy/cloudflare/src/worker.ts を全球版に直したもの）
//   /api/*   → 地点の大陸（つながっている道路網）を担当するコンテナ（FastAPI）。/api を剥がして渡す（issue #9 の A 案）
//   /tiles/* → R2 の PMTiles。Range をそのまま R2 に渡して 206 で返す
//   それ以外 → Static Assets（wrangler.jsonc の run_worker_first に無いパスはここに来ない）
//
// 全球のグラフ（常駐 16 GiB）は Containers の上限 12 GiB に載らないので、つながっている道路網ごとに 3 台に分けた
// （scripts/16_split_world.py）。別の道路網どうしはもともと経路がつながらないので、分けても結果は変わらない。
// 地点 → 台は shard_grid.bin（0.1 度のマス。scripts/18_shard_grid.py）で引き、担当の 1 台だけを起こす
import { Container, getContainer } from "@cloudflare/containers";
import SHARD_GRID from "./shard_grid.bin";

// コンテナはクラスごとにインスタンスの大きさを決める（wrangler.jsonc の containers）。
// 中身は同じで、イメージ（焼き込むグラフ）と大きさだけが違う
class RouteContainer extends Container<Env> {
  defaultPort = 8000;
  // グラフはイメージに焼いてあり、外に取りに行くものはない
  enableInternet = false;

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    // 最後のリクエストからこの時間で寝る。寝ている間は課金されず、次のリクエストで起きる。
    // クラスフィールドに書くと super() の後で上書きされるので、options で渡す
    super(ctx, env, { sleepAfter: env.SLEEP_AFTER || "10m" });
  }
}
export class EurasiaContainer extends RouteContainer {}
export class AmericasContainer extends RouteContainer {}
export class OtherContainer extends RouteContainer {}

interface Env {
  EURASIA: DurableObjectNamespace<EurasiaContainer>;
  AMERICAS: DurableObjectNamespace<AmericasContainer>;
  OTHER: DurableObjectNamespace<OtherContainer>;
  TILES: R2Bucket;
  ASSETS: Fetcher;
  API_LIMITER: RateLimit;
  IP_LIMITER: RateLimit;
  SLEEP_AFTER: string;
}

// shard_grid.bin の値の順（scripts/18_shard_grid.py の SHARDS と同じ）。links・nodes は /api/health に出す規模
// （scripts/16_split_world.py の結果、2026-11-01）
const SHARDS = [
  { name: "eurasia", label: "ユーラシア・アフリカ", links: 42_182_679, nodes: 38_160_154 },
  { name: "americas", label: "南北アメリカ", links: 22_424_614, nodes: 20_712_154 },
  { name: "other", label: "日本・英国・オーストラリアなど", links: 9_630_777, nodes: 9_004_041 },
] as const;
const GRID = new Uint8Array(SHARD_GRID);
const GRID_STEP = 0.1;
const GRID_COLS = 3600;

function shardOf(lat: number, lon: number): number {
  const r = Math.min(Math.floor((lat + 90) / GRID_STEP), 1799);
  const c = Math.min(Math.floor((lon + 180) / GRID_STEP), GRID_COLS - 1);
  return GRID[r * GRID_COLS + c];
}

function container(env: Env, shard: number) {
  const ns = [env.EURASIA, env.AMERICAS, env.OTHER][shard] as DurableObjectNamespace<RouteContainer>;
  return getContainer(ns, "main");
}

const API_PATHS = new Set(["/health", "/reachability", "/route"]);
const LIMITED = new Set(["/reachability", "/route"]);
// 画面がブラウザごとに作るランダム ID。形の合わないものは「ID なし」として同じ枠で数える（KSJ 版 issue #25）
const CLIENT_ID = /^[A-Za-z0-9-]{8,64}$/;
// 寝ていたコンテナは、起動してポートが開いたあともグラフの読み込み中は 503 を返す（api/core.py）。
// 読み込み（手元でユーラシア 14 秒）とイメージの取り出しを待つ
const WAKE_TIMEOUT_MS = 150_000;
const WAKE_RETRY_MS = 2_000;

const TILE_PATH = "/tiles/roads.pmtiles";
// 共有バケット shi-works のキー（KSJ 版と同じ規約: pmtiles/<プロジェクト名>/）。ファイル名の版は Overture のリリース。
// 道路タイルの link_id は全球の番号（コンテナは link_gid.npy で全球の番号に直して返す）。
// データを更新するときは新しい版を別キーで置き、ここを書き換えてイメージと同時にデプロイする
const TILE_KEY = "pmtiles/overture-road-route-search/roads_world_2026-10-21.0.pmtiles";

const json = (status: number, detail: string, headers: HeadersInit = {}) =>
  Response.json({ detail }, { status, headers });

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return api(request, url, env);
    if (url.pathname === TILE_PATH) return tiles(request, env, ctx);
    if (url.pathname.startsWith("/tiles/")) return new Response("Not found", { status: 404 });
    return env.ASSETS.fetch(request);
  },
};

function num(url: URL, key: string): number | null {
  const v = url.searchParams.get(key);
  const n = v === null || v === "" ? NaN : Number(v);
  return Number.isFinite(n) ? n : null;
}

function inRange(lat: number | null, lon: number | null): boolean {
  return lat !== null && lon !== null && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
}

async function api(request: Request, url: URL, env: Env): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return json(405, "GET のみ受け付けます", { Allow: "GET, HEAD" });
  }
  const path = url.pathname.slice("/api".length);
  if (!API_PATHS.has(path)) return json(404, "Not Found");

  if (path === "/health") {
    // ?shard=eurasia などはその台に直接聞く（起こして確かめる）。無ければコンテナを起こさずに Worker が答える。
    // 画面は開いたときにこれを待つので、ここでコンテナを起こすと開くたびに全台が起きてしまう
    const name = url.searchParams.get("shard");
    if (name === null) return health();
    const i = SHARDS.findIndex((s) => s.name === name);
    if (i < 0) return json(400, `shard は ${SHARDS.map((s) => s.name).join("・")} のどれか`);
    url.searchParams.delete("shard");
    url.pathname = path;
    return forward(env, i, new Request(url, request));
  }

  if (LIMITED.has(path)) {
    // 端末ごと（IP＋ID）の枠で連打を止め、ID を毎回変えて投げる相手は IP 全体の枠で止める
    const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const id = request.headers.get("X-Client-Id") ?? "";
    const client = `${ip}|${CLIENT_ID.test(id) ? id : "-"}`;
    if (!(await env.API_LIMITER.limit({ key: client })).success || !(await env.IP_LIMITER.limit({ key: ip })).success) {
      return json(429, "リクエストが多すぎます。10 秒ほど待ってからもう一度お試しください", { "Retry-After": "10" });
    }
  }

  let shard: number;
  if (path === "/reachability") {
    const lat = num(url, "lat"), lon = num(url, "lon");
    if (!inRange(lat, lon)) return json(400, "緯度経度の範囲外です");
    shard = shardOf(lat!, lon!);
  } else {
    const fl = num(url, "from_lat"), fo = num(url, "from_lon"), tl = num(url, "to_lat"), to = num(url, "to_lon");
    if (!inRange(fl, fo) || !inRange(tl, to)) return json(400, "緯度経度の範囲外です");
    shard = shardOf(fl!, fo!);
    const other = shardOf(tl!, to!);
    if (other !== shard) {
      // 別の台 = 別の道路網。コンテナを起こさずに、API と同じ形の「到達不能」を返す
      return Response.json({
        geometry: null,
        summary: { dist_m: 0, time_min: 0, link_count: 0 },
        snap: { origin_m: 0, dest_m: 0 },
        cost: url.searchParams.get("cost") ?? "time",
        use_expressway: url.searchParams.get("use_expressway") !== "false",
        unreachable_reason: `始点と終点が別の道路網に属しています（${SHARDS[shard].label} / ${SHARDS[other].label}）`,
        elapsed_ms: 0,
      });
    }
  }
  url.pathname = path;
  return forward(env, shard, new Request(url, request));
}

// 担当の台に渡す。寝ていれば起き、グラフの読み込み中（503）なら読み終わるまで待って投げ直す
async function forward(env: Env, shard: number, request: Request): Promise<Response> {
  const started = Date.now();
  let res: Response;
  for (;;) {
    res = await container(env, shard).fetch(request.clone());
    if (res.status !== 503 || Date.now() - started > WAKE_TIMEOUT_MS) break;
    await res.body?.cancel();
    await new Promise((r) => setTimeout(r, WAKE_RETRY_MS));
  }
  const out = new Response(res.body, res);
  out.headers.set("X-Route-Shard", SHARDS[shard].name);
  out.headers.set("X-Wake-Ms", String(Date.now() - started));
  return out;
}

function health(): Response {
  return Response.json({
    status: "ok",
    // 画面はこれが true になるまで地図を触らせない。コンテナは最初の探索で起きる（寝ていれば 1 分前後かかる）
    graph_loaded: true,
    uptime_seconds: 0,
    features: [],
    case: "world（3 台）",
    speed: "legal",
    links: SHARDS.reduce((a, s) => a + s.links, 0),
    nodes: SHARDS.reduce((a, s) => a + s.nodes, 0),
    shards: SHARDS.map((s) => ({ name: s.name, label: s.label, links: s.links, nodes: s.nodes })),
  });
}

// ---------------------------------------------------------------- 道路タイル（KSJ 版と同じ）
// R2 から毎回読むと Range 1 回に約 0.2 秒掛かり、地図 1 画面で数十回読むので描画が遅い（KSJ 版 issue #4）。
// 読んだ範囲をこのデータセンターの Cache API に置き、2 回目以降はそこから返す。Cache API は無料。
// cache.put は 206 を受け付けないので「URL＋範囲」をキーに 200 で保存し、返すときに 206 に戻す。
// キーに TILE_KEY（版入り）を含めるので、データ更新でキーを変えれば古いキャッシュは読まれない
const TILE_CACHE_SECONDS = 86400;
// PMTiles のクライアントが読むのはディレクトリ（16 KB 程度）とタイル。これより大きい範囲は置かない
const TILE_CACHE_MAX_BYTES = 4 * 1024 * 1024;

async function tiles(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const m = request.method === "GET" ? /^bytes=(\d+)-(\d+)$/.exec(request.headers.get("Range") ?? "") : null;
  if (m && !request.headers.has("If-None-Match")) {
    const start = Number(m[1]), end = Number(m[2]);
    if (end >= start && end - start + 1 <= TILE_CACHE_MAX_BYTES) {
      return cachedRange(request, env, ctx, start, end);
    }
  }
  return tilesFromR2(request, env);
}

async function cachedRange(request: Request, env: Env, ctx: ExecutionContext, start: number, end: number): Promise<Response> {
  const keyUrl = new URL(request.url);
  keyUrl.pathname = `/__tile-cache/${TILE_KEY}`;
  keyUrl.search = `?r=${start}-${end}`;
  const key = new Request(keyUrl.toString());
  const cache = caches.default;

  const hit = await cache.match(key);
  if (hit) {
    const etag = hit.headers.get("ETag");
    const ifMatch = request.headers.get("If-Match");
    if (ifMatch && etag && ifMatch !== etag) return new Response(null, { status: 412 });
    return rangeResponse(hit.body, hit.headers, "HIT");
  }

  const obj = await env.TILES.get(TILE_KEY, { range: { offset: start, length: end - start + 1 }, onlyIf: request.headers });
  if (!obj) return new Response("Not found", { status: 404 });
  const headers = tileHeaders(obj);
  if (!("body" in obj)) return new Response(null, { status: 412, headers });
  const last = Math.min(end, obj.size - 1);
  headers.set("Content-Range", `bytes ${start}-${last}/${obj.size}`);
  const buf = await obj.arrayBuffer();

  const stored = new Headers(headers);
  stored.set("Cache-Control", `public, max-age=${TILE_CACHE_SECONDS}`);
  ctx.waitUntil(cache.put(key, new Response(buf, { status: 200, headers: stored })));
  return rangeResponse(buf, headers, "MISS");
}

function rangeResponse(body: BodyInit | null, src: Headers, cacheStatus: "HIT" | "MISS"): Response {
  const headers = new Headers(src);
  headers.set("Cache-Control", "public, max-age=3600");
  headers.set("X-Tile-Cache", cacheStatus);
  headers.delete("Content-Length");
  return new Response(body, { status: 206, headers });
}

async function tilesFromR2(request: Request, env: Env): Promise<Response> {
  if (request.method === "HEAD") {
    const head = await env.TILES.head(TILE_KEY);
    if (!head) return new Response("Not found", { status: 404 });
    const headers = tileHeaders(head);
    headers.set("Content-Length", String(head.size));
    return new Response(null, { headers });
  }
  if (request.method !== "GET") return new Response("Method not allowed", { status: 405 });

  const obj = await env.TILES.get(TILE_KEY, { range: request.headers, onlyIf: request.headers });
  if (!obj) return new Response("Not found", { status: 404 });
  const headers = tileHeaders(obj);
  if (!("body" in obj)) {
    return new Response(null, { status: request.headers.has("If-None-Match") ? 304 : 412, headers });
  }
  const r = obj.range as { offset?: number; length?: number; suffix?: number } | undefined;
  if (!request.headers.has("Range") || !r) {
    return new Response(obj.body, { headers });
  }
  const start = r.suffix !== undefined ? obj.size - r.suffix : (r.offset ?? 0);
  const length = r.suffix !== undefined ? r.suffix : (r.length ?? obj.size - start);
  headers.set("Content-Range", `bytes ${start}-${start + length - 1}/${obj.size}`);
  headers.set("Content-Length", String(length));
  return new Response(obj.body, { status: 206, headers });
}

function tileHeaders(obj: R2Object): Headers {
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("ETag", obj.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Content-Type", "application/octet-stream");
  headers.set("Cache-Control", "public, max-age=3600");
  return headers;
}
