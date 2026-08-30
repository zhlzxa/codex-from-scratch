import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Two ways to run this, and the proxy is what makes the first one work.
//
//   npm run dev     -> Vite on 5173 with hot reload, /api and /ws proxied to
//                      the Python server on 8000. Two processes, instant reload.
//   npm run build   -> dist/, which `minicodex serve` mounts at / itself.
//                      One process, one port, nothing to install to run it.
//
// `ws: true` on the /ws entry is not optional: without it the proxy answers the
// upgrade request with a normal HTTP response and the socket never opens, which
// looks exactly like a server that accepted the connection and then went quiet.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    // Chapter 15's argument about artefacts, one layer up: a stack trace from a
    // minified bundle with no map is a bug report nobody can act on, and the
    // maps are not served to anyone who does not ask for them.
    sourcemap: true,
  },
});
