import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Runes 記法を強制
				runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
			},
			// SPA なので未知のパスは index.html（+page.ts で ssr = false）
			adapter: adapter({ fallback: 'index.html' })
		})
	],
	// API は同一オリジンの /api。Vite が /api を剥がして 8010（KSJ 版の 8000 とぶつからないように）に渡す。
	// localhost だと Windows の Node は ::1 に繋ぎ、127.0.0.1 で待つ uvicorn に届かないので IPv4 で書く
	server: {
		proxy: { '/api': { target: 'http://127.0.0.1:8010', rewrite: (p) => p.replace(/^\/api/, '') } }
	}
});
