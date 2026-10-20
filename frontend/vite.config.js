import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // SSE 要关掉缓冲，否则 /stream 的进度会攒到结束才一次性吐出来
      '/api': { target: 'http://127.0.0.1:8123', changeOrigin: true },
    },
  },
})
