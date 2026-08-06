// vite.config.js — Cairn v2 前端
// 说明：本前端为「原生 ESM，无运行期 npm 依赖」，可由 Cairn Server 直接静态托管
//（_mount_static 以 static/index.html 存在与否挂载，index.html 即源码入口，开箱即用）。
// `npm run build` 产出 dist/ 为生产优化产物（minify + 相对 base），
// 部署时若以构建产物为准，可将 dist/ 内容拷回 static/ 根（或按需调整 outDir）。
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
  },
  server: {
    port: 5173,
    // Vite 开发代理 → Cairn Server（默认 8000；独立部署时改 CAIRN_PORT 并在浏览器以
    // window.CAIRN_API_BASE 覆盖，或改此处 proxy target）
    proxy: {
      '/engagements': 'http://127.0.0.1:8000',
      '/tasks': 'http://127.0.0.1:8000',
      '/projects': 'http://127.0.0.1:8000',
      '/settings': 'http://127.0.0.1:8000',
    },
  },
});
