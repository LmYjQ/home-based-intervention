import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端只有这一套，固定 5173。
//
// 代理打真后端（`make dev`，8000 端口）。
// strictPort：端口被占时直接报错，而不是静默跳到别的端口 ——
// 静默跳端口会让人对着旧服务调试，还以为是自己改的东西没生效。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
