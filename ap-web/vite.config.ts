// coverage-gate smoke test: no-op, safe to revert
import { execFileSync } from "node:child_process";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";

const OMNIGENT_URL = process.env.OMNIGENT_URL ?? "http://localhost:6767";

let cachedToken: string | null | undefined;

function resolveToken(host: string): string | null {
  if (cachedToken !== undefined) return cachedToken;

  if (process.env.OMNIGENT_AUTH_TOKEN) {
    cachedToken = process.env.OMNIGENT_AUTH_TOKEN;
    return cachedToken;
  }

  try {
    const output = execFileSync(
      "databricks",
      ["auth", "token", "--host", host, "--output", "json"],
      {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const tokenResponse = JSON.parse(output) as { access_token?: string };
    cachedToken = tokenResponse.access_token ?? null;
  } catch {
    cachedToken = null;
  }

  return cachedToken;
}

function configureProxy(target: string, useAuth: boolean): NonNullable<ProxyOptions["configure"]> {
  const parsed = new URL(target);
  const host = parsed.origin;
  // The URL pathname becomes a prefix prepended to every proxied request.
  // e.g. OMNIGENT_URL=https://host.com/api/2.0/omnigent means the browser's
  // /v1/sessions is rewritten to /api/2.0/omnigent/v1/sessions before forwarding.
  const basePath = parsed.pathname.replace(/\/$/, "");

  return (proxy) => {
    proxy.on("proxyReq", (proxyReq) => {
      if (basePath) proxyReq.path = `${basePath}${proxyReq.path}`;
      if (useAuth) {
        const token = resolveToken(host);
        if (token) proxyReq.setHeader("Authorization", `Bearer ${token}`);
      }
    });

    proxy.on("proxyReqWs", (proxyReq) => {
      if (basePath) proxyReq.path = `${basePath}${proxyReq.path}`;
      if (useAuth) {
        const token = resolveToken(host);
        if (token) proxyReq.setHeader("Authorization", `Bearer ${token}`);
      }
    });

    proxy.on("proxyRes", (proxyRes, _req, res) => {
      const contentType = proxyRes.headers["content-type"] ?? "";
      if (typeof contentType === "string" && contentType.includes("text/event-stream")) {
        // http-proxy applies upstream headers after its own proxyRes listener
        // runs. Defer flushing until after those headers have been copied.
        setImmediate(() => res.flushHeaders());
      }
    });
  };
}

function createProxyConfig(target: string, useAuth: boolean): Record<string, ProxyOptions> {
  const origin = new URL(target).origin;
  const configure = configureProxy(target, useAuth);

  return {
    "/v1": {
      target: origin,
      changeOrigin: true,
      ws: true,
      configure,
    },
    "/api": {
      target: origin,
      changeOrigin: true,
      configure,
    },
    "/auth": {
      target: origin,
      changeOrigin: true,
      configure,
    },
    "/health": {
      target: origin,
      changeOrigin: true,
      configure,
    },
  };
}

const parsed = new URL(OMNIGENT_URL);
const useAuth =
  !!process.env.OMNIGENT_AUTH_TOKEN ||
  parsed.hostname.endsWith(".databricks.com") ||
  parsed.hostname.endsWith(".azuredatabricks.net");

if (useAuth) {
  const token = resolveToken(parsed.origin);
  if (token) {
    console.log(`[dev-proxy] target=${OMNIGENT_URL} (authenticated)`);
  } else {
    console.error(
      `\n[dev-proxy] ERROR: No auth token for ${parsed.origin}.\n` +
        `  Set OMNIGENT_AUTH_TOKEN or run:  databricks auth login --host ${parsed.origin}\n`,
    );
    process.exit(1);
  }
} else {
  console.log(`[dev-proxy] target=${OMNIGENT_URL}`);
}

const proxyConfig = createProxyConfig(OMNIGENT_URL, useAuth);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    coverage: {
      provider: "v8",
      // With `include` set, vitest counts every matching source file (untested
      // ones as 0%), so the total reflects the whole frontend — parity with the
      // backend's --cov=omnigent, not just files a test happened to import.
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/*.d.ts",
        "src/test-setup.ts",
        // Vendored UI kit, not product code (see tests/e2e_ui/COVERAGE_GAPS.md).
        "src/components/ai-elements/**",
      ],
      reportsDirectory: "./coverage",
      // text-summary: human-readable console line; json-summary: machine-
      // readable coverage/coverage-summary.json that CI distills to total.txt.
      reporter: ["text-summary", "json-summary"],
    },
  },
  server: {
    proxy: proxyConfig,
  },
  build: {
    outDir: path.resolve(__dirname, "../omnigent/server/static/web-ui"),
    emptyOutDir: true,
  },
});
