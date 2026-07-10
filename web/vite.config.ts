import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin, ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";

import { computeBuildVersion } from "./src/lib/buildVersion";

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

// PWA web app manifest. Static (the app's identity doesn't change per build);
// emitted by the plugin below — NOT placed in `public/`, because `public/` is
// copied into the embed-island build too (vite.embed.config.ts), and the embed
// must never ship a manifest/SW (it loads inside a host app's origin). `id` is
// pinned independent of a future `start_url` change so the browser keeps
// treating reinstalls/updates as the same app.
const PWA_MANIFEST = {
  id: "/",
  name: "Omnigent",
  short_name: "Omnigent",
  description: "Omnigent — a common layer over coding agents.",
  start_url: "/",
  scope: "/",
  display: "standalone",
  orientation: "any",
  theme_color: "#0d1218",
  background_color: "#0d1218",
  icons: [
    { src: "/pwa-192.png", sizes: "192x192", type: "image/png" },
    { src: "/pwa-512.png", sizes: "512x512", type: "image/png" },
    { src: "/pwa-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
  ],
};

/**
 * Emit the PWA assets for the standalone build: `version.json`,
 * `manifest.webmanifest`, and a `sw.js` whose `__BUILD_VERSION__` token is
 * replaced with a fingerprint of this build's hashed JS/CSS outputs
 * (`computeBuildVersion`). That fingerprint makes `sw.js` change on every
 * code/style deploy, which is what fires the in-app update prompt. Registered
 * ONLY here (not in `vite.embed.config.ts`), so the embed island ships neither
 * a service worker nor a manifest.
 *
 * In dev, `generateBundle` doesn't run, so the dev server serves the manifest
 * via middleware (otherwise the `index.html` link 404s) — but no `sw.js`: there
 * is deliberately no service worker in dev (see `useServiceWorkerUpdate`).
 */
function emitPwaAssets(): Plugin {
  return {
    name: "emit-pwa-assets",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url !== "/manifest.webmanifest") return next();
        res.setHeader("Content-Type", "application/manifest+json");
        res.end(JSON.stringify(PWA_MANIFEST));
      });
    },
    generateBundle(_options, bundle) {
      const build = computeBuildVersion(Object.keys(bundle));
      const swSource = readFileSync(path.resolve(__dirname, "sw-src/sw.js"), "utf8");
      // Fail the build loudly rather than ship a service worker with no
      // per-build fingerprint — a missing token would silently leave `sw.js`
      // byte-identical across deploys, so the update prompt would never fire.
      if (!swSource.includes("__BUILD_VERSION__")) {
        this.error("sw-src/sw.js is missing the __BUILD_VERSION__ token; cannot fingerprint sw.js");
      }
      this.emitFile({
        type: "asset",
        fileName: "version.json",
        source: JSON.stringify({ build }),
      });
      this.emitFile({
        type: "asset",
        fileName: "manifest.webmanifest",
        source: JSON.stringify(PWA_MANIFEST),
      });
      this.emitFile({
        type: "asset",
        fileName: "sw.js",
        // replaceAll (not replace): if a second reference to the token is ever
        // added, replace() would leave it raw and break the cache name.
        source: swSource.replaceAll("__BUILD_VERSION__", build),
      });
    },
  };
}

// Safari < 16.4 cannot parse regex lookbehind; these dependency regexes would
// otherwise throw there, at module scope during boot or on the first rendered
// markdown message (#1978):
// - marked probes lookbehind support with `new RegExp("(?<=1)(?<!1)")` inside
//   try/catch, but rolldown constant-folds the probe to `true`; route the
//   constructor through `globalThis` so it stays a runtime check.
// - remend builds its single-tilde repair regex at module scope with no
//   guard; fall back to a never-matching regex so the repair no-ops.
// - mdast-util-gfm-autolink-literal's email regex is constructed when a GFM
//   message renders; fall back to a never-matching regex (plain-text emails
//   just don't autolink).
const LOOKBEHIND_REWRITES: [string, string][] = [
  ['new RegExp("(?<=1)(?<!1)")', 'new globalThis.RegExp("(?<=1)(?<!1)")'],
  [
    'new RegExp("(?<=[\\\\p{L}\\\\p{N}_])~(?!~)(?=[\\\\p{L}\\\\p{N}_])","gu")',
    '(() => { try { return new globalThis.RegExp("(?<=[\\\\p{L}\\\\p{N}_])~(?!~)(?=[\\\\p{L}\\\\p{N}_])", "gu"); } catch { return /(?!)/gu; } })()',
  ],
  [
    "/(?<=^|\\s|\\p{P}|\\p{S})([-.\\w+]+)@([-\\w]+(?:\\.[-\\w]+)+)/gu",
    '(() => { try { return new globalThis.RegExp("(?<=^|\\\\s|\\\\p{P}|\\\\p{S})([-.\\\\w+]+)@([-\\\\w]+(?:\\\\.[-\\\\w]+)+)", "gu"); } catch { return /(?!)/gu; } })()',
  ],
];
const LOOKBEHIND_REWRITE_MODULES = [
  "/node_modules/marked/",
  "/node_modules/remend/",
  "/node_modules/mdast-util-gfm-autolink-literal/",
];

function isLookbehindRewriteModule(id: string): boolean {
  const normalizedId = id.replaceAll("\\", "/");
  return LOOKBEHIND_REWRITE_MODULES.some((modulePath) => normalizedId.includes(modulePath));
}

function safariLookbehindWorkarounds(): Plugin {
  return {
    name: "safari-lookbehind-workarounds",
    transform(code, id) {
      if (!isLookbehindRewriteModule(id)) return;

      let out = code;
      for (const [from, to] of LOOKBEHIND_REWRITES) {
        out = out.replaceAll(from, to);
      }
      if (out === code) return;
      return { code: out, map: null };
    },
  };
}

export default defineConfig({
  plugins: [emitPwaAssets(), safariLookbehindWorkarounds(), react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Scope discovery to src/ — the web suite lives there. Without this,
    // vitest's default glob descends into the nested electron package and
    // tries to run its node:test files (which aren't vitest suites).
    include: ["src/**/*.{test,spec}.?(c|m)[jt]s?(x)"],
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
    // default baseline is Safari 16.4+; iPadOS 15 can't parse dep regex lookbehinds (#1978)
    target: ["chrome111", "edge111", "firefox114", "safari15", "ios15"],
    outDir: path.resolve(__dirname, "../omnigent/server/static/web-ui"),
    emptyOutDir: true,
  },
});
