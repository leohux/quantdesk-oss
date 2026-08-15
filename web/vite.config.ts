import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/account': 'http://127.0.0.1:8000',
      '/positions': 'http://127.0.0.1:8000',
      '/orders': 'http://127.0.0.1:8000',
      '/backtest': 'http://127.0.0.1:8000',
      '/signal': 'http://127.0.0.1:8000',
    },
  },
})
