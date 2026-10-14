// Build + preview config for the e2e embed host page (`e2e-embed-host/`): the preview
// proxies two Servers behind one origin, authenticated as the host's signed-in user.
// The harness (tests/e2e_ui/embed/_embed_host_harness.py) sets the OMNIGENT_E2E_* vars;
// a build without them lands in `dist-e2e-embed-host` with the harness's default users.
import fs from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin, type ProxyOptions } from "vite";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} must be set; the e2e harness sets it`);
  return value;
}

const OUT_DIR = path.resolve(
  __dirname,
  process.env.OMNIGENT_E2E_HOST_DIST || "dist-e2e-embed-host",
);
const USERS = {
  a: process.env.OMNIGENT_E2E_SERVER_A_USER || "alice@example.test",
  b: process.env.OMNIGENT_E2E_SERVER_B_USER || "bob@example.test",
};

function serverProxy(prefix: string, target: string, user: string): Record<string, ProxyOptions> {
  return {
    [prefix]: {
      target,
      changeOrigin: true,
      ws: true,
      rewrite: (requestPath) => requestPath.slice(prefix.length) || "/",
      configure: (proxy) => {
        proxy.on("proxyReq", (proxyReq) => proxyReq.setHeader("X-Forwarded-Email", user));
        proxy.on("proxyReqWs", (proxyReq) => proxyReq.setHeader("X-Forwarded-Email", user));
        proxy.on("proxyRes", (proxyRes, _req, res) => {
          if (String(proxyRes.headers["content-type"] ?? "").includes("text/event-stream")) {
            setImmediate(() => res.flushHeaders());
          }
        });
      },
    },
  };
}

// Only the preview serves the Servers; the build runs before they exist.
function proxies(): Record<string, ProxyOptions> {
  return {
    ...serverProxy("/server-a", required("OMNIGENT_E2E_SERVER_A_URL"), USERS.a),
    ...serverProxy("/server-b", required("OMNIGENT_E2E_SERVER_B_URL"), USERS.b),
  };
}

// The Vite root stays at `web/` so Tailwind scans `src/`; the host page is in a
// subdirectory, so its index.html is hoisted for the preview SPA fallback.
function hoistHostIndex(): Plugin {
  return {
    name: "hoist-e2e-embed-host-index",
    closeBundle() {
      fs.copyFileSync(
        path.join(OUT_DIR, "e2e-embed-host", "index.html"),
        path.join(OUT_DIR, "index.html"),
      );
    },
  };
}

export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss(), hoistHostIndex()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  define: {
    OMNIGENT_E2E_USERS: JSON.stringify(USERS),
  },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "e2e-embed-host/index.html"),
    },
    minify: false,
    reportCompressedSize: false,
  },
  server: { proxy: command === "serve" ? proxies() : {} },
  preview: { proxy: command === "serve" ? proxies() : {} },
}));
