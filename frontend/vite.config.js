import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// The VITE_API_BASE_URL lets the operator point the browser at an absolute
// URL (e.g. http://localhost:4015) or at a reverse-proxy path (e.g. /api).
// When unset we default to http://localhost:4015.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const apiBase = env.VITE_API_BASE_URL || 'http://localhost:4015';

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        // Dev convenience: visiting /api/* during `vite dev` forwards to backend.
        '/api': {
          target: apiBase,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
    },
    define: {
      __API_BASE__: JSON.stringify(apiBase),
    },
  };
});
