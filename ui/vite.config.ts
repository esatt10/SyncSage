import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SyncSage indexing container stays its own workload; this app is built to a
// static bundle and talks to it purely over HTTP. In dev we proxy API calls to a
// locally running SyncSage so the bundle can use same-origin paths.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Forward the SyncSage HTTP API to the running container during dev.
      "/api": {
        target: process.env.SYNCSAGE_API_BASE ?? "http://localhost:8765",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
