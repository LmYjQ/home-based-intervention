import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 端口跟着后端走。
//
// 默认打真后端（`make dev`，8000 端口）。想脱离数据库单独调前端，
// 用 `npm run dev:mock` —— 代理指到 mock_server.py（8010 端口）。
//
// 5173/5174 被旧的 frontend/ 占着（那套是冻结遗留的，别混），
// 所以这个测试台固定用 5176。strictPort 是为了端口被占时直接报错，
// 而不是静默跳到别的端口、让人对着旧服务调试。
const MOCK = process.env.MOCK === '1'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5176,
    strictPort: true,
    proxy: {
      '/api': { target: MOCK ? 'http://127.0.0.1:8010' : 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: MOCK ? 'http://127.0.0.1:8010' : 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})