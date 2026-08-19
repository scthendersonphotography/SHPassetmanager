import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built bundle is served by the FastAPI server at /ui — see main.py.
// Assets use relative paths so the bundle works at any mount point.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    // Dev-only: proxy API calls to a running Pi TX daemon
    proxy: {
      "/data": "http://127.0.0.1:8000",
      "/cmd": "http://127.0.0.1:8000",
      "/telemetry": "http://127.0.0.1:8000",
    },
  },
});
