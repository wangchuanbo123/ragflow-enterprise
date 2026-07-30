import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
// Vite 配置：开发环境将 /api 代理到 FastAPI（默认 8000）
export default defineConfig({
    plugins: [vue()],
    server: {
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
        },
    },
});
