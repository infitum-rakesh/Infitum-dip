import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    // Bind to all network interfaces (not just localhost) so other
    // machines on the LAN can reach this dev server at this machine's IP —
    // e.g. http://192.168.1.2:5173. The /api proxy below still runs
    // server-side against localhost:8080 on THIS machine either way, so
    // clients never need to know the backend's address separately.
    host: true,
    // Vite rejects requests whose Host header isn't localhost/the dev
    // machine's own IP unless explicitly allowed (DNS-rebinding
    // protection). A Cloudflare quick tunnel serves this app through a
    // random *.trycloudflare.com subdomain, so allow that suffix.
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      '/api': {
        // Overridable via BACKEND_PORT for running a second, isolated dev
        // instance against a different backend port (e.g. live-verifying
        // in-progress backend changes without restarting the shared :8080
        // dev server another session may be using). Defaults to 8080,
        // unchanged from before.
        target: `http://localhost:${process.env.BACKEND_PORT || 8080}`,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
