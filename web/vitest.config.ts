import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: { environment: 'jsdom', include: ['tests/unit/**/*.test.ts'], setupFiles: ['tests/setup.ts'], clearMocks: true },
})
