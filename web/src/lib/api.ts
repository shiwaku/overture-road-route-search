// FastAPI（api/main.py）の型付きクライアント。../ksj-route-search-api の web/src/lib/api.ts から移植（ブックマークは外した）
// 同一オリジンの /api。Vite の proxy が /api を剥がして FastAPI に渡す
export const API = '/api';

export type RoadClass = 'auto' | 'all' | 'trunk' | 'major';
export type Tier = 'ok' | 'heavy';

export interface Health {
	status: 'ok' | 'loading' | 'error';
	graph_loaded: boolean;
	case?: string;
	links?: number;
	nodes?: number;
	load_seconds?: number;
	error?: string;
}

export interface Reachability {
	origin: { lat: number; lon: number };
	snap: { lat: number; lon: number; dist_m: number };
	limit_min: number;
	use_expressway: boolean;
	road_class_applied: RoadClass;
	tier: Tier;
	counts: Record<'all' | 'trunk' | 'major', number>;
	count: number;
	nodes_reached: number;
	dijkstra_ms: number;
	elapsed_ms: number;
	// JSON では number[]、format=bin では型付き配列（Uint32Array / Float32Array）。読む側は添字と length だけ使う
	link_ids: ArrayLike<number>;
	costs: ArrayLike<number>;
}

export interface Route {
	geometry: { type: 'LineString'; coordinates: [number, number][] } | null;
	summary: { dist_m: number; time_min: number; link_count: number; nodes_searched?: number; tries?: number };
	snap: { origin_m: number; dest_m: number };
	unreachable_reason: string | null;
	elapsed_ms: number;
}

export class ApiError extends Error {
	constructor(public status: number, detail: string) {
		super(detail);
	}
}

async function fetchOk(path: string, init?: RequestInit): Promise<Response> {
	const res = await fetch(API + path, init);
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const d = body.detail;
		throw new ApiError(res.status, typeof d === 'string' ? d : JSON.stringify(d ?? res.statusText));
	}
	return res;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetchOk(path, init);
	return res.status === 204 ? (undefined as T) : res.json();
}

// 到達圏のバイナリ形式（api/core.py の _binary_body）
// [u32 ヘッダ長 H][ヘッダ JSON][link_id の差分 u32 × count][コスト u16 × count]（リトルエンディアン）
function decodeReachability(buf: ArrayBuffer): Reachability {
	const h = new DataView(buf).getUint32(0, true);
	const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, h)));
	const n: number = head.count;
	const off = 4 + h; // ヘッダは 4 の倍数まで埋めてあるので Uint32Array をそのまま被せられる
	const deltas = new Uint32Array(buf, off, n);
	const q = new Uint16Array(buf, off + 4 * n, n);
	const linkIds = new Uint32Array(n);
	const costs = new Float32Array(n);
	const unit: number = head.cost_unit;
	for (let i = 0, id = 0; i < n; i++) {
		id += deltas[i];
		linkIds[i] = id;
		costs[i] = q[i] * unit;
	}
	return { ...head, link_ids: linkIds, costs };
}

const q = (o: Record<string, string | number | boolean>) =>
	'?' + Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');

export const api = {
	health: () => call<Health>('/health'),
	reachability: async (p: { lat: number; lon: number; limit_min: number; use_expressway: boolean; road_class?: RoadClass }) => {
		const res = await fetchOk('/reachability' + q({ road_class: 'auto', ...p, format: 'bin' }));
		return decodeReachability(await res.arrayBuffer());
	},
	route: (p: { from_lat: number; from_lon: number; to_lat: number; to_lon: number; use_expressway: boolean }) =>
		call<Route>('/route' + q(p))
};
