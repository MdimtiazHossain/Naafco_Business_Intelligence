import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Ports 8000 and 5173 are taken by other applications on this machine, so the
  // web app is pinned to 5183 (see frontend/.env.local, which points the client
  // at the API on 8010). strictPort makes a clash fail loudly rather than let
  // Vite drift to the next free port and silently contradict the documented URL.
  server: {
    port: 5183,
    strictPort: true,
  },
})
