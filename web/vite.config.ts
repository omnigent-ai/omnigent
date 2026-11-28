import { execFile } from "node:child_process";
import { validateHeaderName, validateHeaderValue } from "node:http";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin, ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";
import { databricksProfileForWorkspace } from "./vite.databricksProfile";
import { shikiManualChunk } from "./vite.shiki";
import { streamdownManualChunk } from "./vite.streamdown";
import { devServerWorkerExports, workerBuildOptions } from "./vite.workerExports";

// Databricks workspace-hosted omnigent is mounted behind the api-proxy at this
// path; a local / self-hosted server mounts at the root. Mirrors the Python
// WORKSPACE_API_PATH (omnigent/cli_auth.py) so both sides agree on the shape of
// a workspace URL.
const WORKSPACE_API_PATH = "/api/2.0/omnigent";
// The workspace web-UI URL the CLI displays; mirrors WORKSPACE_UI_PATH in omnigent/util/server_url.py.
const WORKSPACE_UI_PATH = "/omnigent";
// Routes a request to one workspace on a host that fronts many; mirrors
// DATABRICKS_ORG_ID_HEADER in omnigent/cli_auth.py.
const DATABRICKS_ORG_ID_HEADER = "X-Databricks-Org-Id";

function isWorkspaceHost(hostname: string): boolean {
  return hostname.endsWith(".databricks.com") || hostname.endsWith(".azuredatabricks.net");
}

// Resolve the proxy target. Point OMNIGENT_URL at a bare workspace origin
// (https://<ws>.databricks.com) and the api-proxy mount is filled in
// automatically, so the browser's relative /v1/... paths reach the
// workspace-hosted server without hand-typing the /api/2.0/omnigent prefix. An
// explicit non-root path is respected (a custom mount, or a local server).
function resolveTarget(raw: string): { target: string; orgId: string | null } {
  const url = new URL(raw);
  // A `?o=<org-id>` workspace selector moves into the org-id routing header.
  const orgId = url.searchParams.get("o");
  if (orgId !== null && !/^\d+$/.test(orgId)) {
    throw new Error("OMNIGENT_URL ?o= must be a numeric workspace org id");
  }
  url.search = "";
  url.hash = "";
  // A pasted web-UI URL (`<ws>/omnigent`) means the same server as the bare origin.
  const root = url.pathname.replace(/\/$/, "");
  if (isWorkspaceHost(url.hostname) && (root === "" || root === WORKSPACE_UI_PATH)) {
    url.pathname = WORKSPACE_API_PATH;
  }
  return { target: url.toString().replace(/\/$/, ""), orgId };
}

const { target: OMNIGENT_URL, orgId: ORG_ID } = resolveTarget(
  process.env.OMNIGENT_URL ?? "http://localhost:6767",
);
// Mint tokens from this Databricks CLI profile instead of matching by host.
const DATABRICKS_PROFILE = process.env.OMNIGENT_DATABRICKS_PROFILE || undefined;

let cliToken: { value: string | null; expiresAt: number } = { value: null, expiresAt: 0 };
let refreshAt = 0;
let refreshing: Promise<void> | undefined;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
let refreshFailing = false;

function reportRefreshFailure(message: string) {
  if (!refreshFailing) console.warn(`[dev-proxy] could not get a Databricks token: ${message}`);
  refreshFailing = true;
}

// Mints a CLI token in the background and again shortly before it expires, even
// while idle; requests keep the current token meanwhile. Failures retry after 10s.
function refreshToken(host: string): Promise<void> {
  const target = new URL(host);
  // A Databricks token never travels in cleartext, except to this machine.
  if (
    target.protocol === "http:" &&
    !["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)
  ) {
    reportRefreshFailure(`refusing to send it over plain http to ${host}; use https`);
    return Promise.resolve();
  }
  refreshing ??= new Promise((resolve) => {
    const profile =
      DATABRICKS_PROFILE ?? (ORG_ID ? databricksProfileForWorkspace(host, ORG_ID) : undefined);
    const selector = profile ? ["--profile", profile] : ["--host", host];
    const args = ["auth", "token", ...selector, "--output", "json"];
    // The CLI's own timeout allows for a browser login; the dev server shouldn't wait that long.
    const child = execFile(
      "databricks",
      args,
      { encoding: "utf8", timeout: 30_000 },
      (error, stdout) => {
        const now = Date.now();
        refreshAt = now + 10_000;
        try {
          if (error) throw error;
          const tokenResponse = JSON.parse(stdout) as { access_token?: string; expiry?: string };
          if (!tokenResponse.access_token)
            throw new Error("databricks auth token returned no token");
          // CLI OAuth tokens last about an hour; one without a parseable expiry is re-read in minutes.
          const expiresAt = Date.parse(tokenResponse.expiry ?? "") || now + 5 * 60_000;
          cliToken = { value: tokenResponse.access_token, expiresAt };
          refreshAt = Math.max(refreshAt, expiresAt - 60_000);
          refreshFailing = false;
        } catch (failure) {
          // Keep the current token, e.g. until the user logs in again.
          reportRefreshFailure((failure as Error).message.trim());
        }
        clearTimeout(refreshTimer);
        // setTimeout fires at once for delays past ~24.8 days; requests still refresh when due.
        refreshTimer = setTimeout(() => resolveToken(host), Math.min(refreshAt - now, 2 ** 31 - 1));
        refreshTimer.unref();
        refreshing = undefined;
        resolve();
      },
    );
    child.stdin?.end();
  });
  return refreshing;
}

function resolveToken(host: string): string | null {
  if (process.env.OMNIGENT_AUTH_TOKEN) return process.env.OMNIGENT_AUTH_TOKEN;
  const now = Date.now();
  if (now >= refreshAt) void refreshToken(host);
  return now < cliToken.expiresAt ? cliToken.value : null;
}

function configureProxy(target: string, useAuth: boolean): NonNullable<ProxyOptions["configure"]> {
  const parsed = new URL(target);
  const host = parsed.origin;
  // The URL pathname becomes a prefix prepended to every proxied request.
  // e.g. OMNIGENT_URL=https://host.com/api/2.0/omnigent means the browser's
  // /v1/sessions is rewritten to /api/2.0/omnigent/v1/sessions before forwarding.
  const basePath = parsed.pathname.replace(/\/$/, "");

  return (proxy) => {
    // Start minting at server start too; `vite preview` skips the startup check.
    if (useAuth) resolveToken(host);

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
  const byOrigin = JSON.parse(process.env.OMNIGENT_PROXY_HEADERS || "{}");
  if (!byOrigin || typeof byOrigin !== "object" || Array.isArray(byOrigin)) {
    throw new Error("OMNIGENT_PROXY_HEADERS must be a JSON object keyed by server origin");
  }
  const configured = byOrigin[origin] === undefined ? {} : byOrigin[origin];
  if (!configured || typeof configured !== "object" || Array.isArray(configured)) {
    throw new Error("OMNIGENT_PROXY_HEADERS entries must be header objects");
  }
  const headers = { ...(ORG_ID ? { [DATABRICKS_ORG_ID_HEADER]: ORG_ID } : {}), ...configured };
  for (const [name, value] of Object.entries(headers)) {
    validateHeaderName(name);
    if (typeof value !== "string") {
      throw new Error("OMNIGENT_PROXY_HEADERS header values must be strings");
    }
    validateHeaderValue(name, value);
  }

  return {
    "/v1": {
      target: origin,
      headers,
      changeOrigin: true,
      ws: true,
      configure,
    },
    "/api": {
      target: origin,
      headers,
      changeOrigin: true,
      configure,
    },
    "/auth": {
      target: origin,
      headers,
      changeOrigin: true,
      configure,
    },
    "/health": {
      target: origin,
      headers,
      changeOrigin: true,
      configure,
    },
    // The server's version manifest (/.well-known/omnigent.json), which the
    // desktop shell reads to learn what it's talking to. Without this the dev
    // server answers with the SPA's index.html, and the shell — which rightly
    // refuses to parse HTML as a manifest — sees every dev server as
    // "pre-manifest", making the capability invisible in local development.
    "/.well-known": {
      target: origin,
      headers,
      changeOrigin: true,
      configure,
    },
  };
}

const parsed = new URL(OMNIGENT_URL);
const useAuth =
  !!process.env.OMNIGENT_AUTH_TOKEN || !!DATABRICKS_PROFILE || isWorkspaceHost(parsed.hostname);

// A Databricks workspace-hosted server sits behind the multi-replica sharding
// layer, so the dev bundle must emit the host_id slice key on host-scoped
// traffic — same as the embedded (managed) UI — or requests scatter across
// replicas and a host's runner tunnel becomes unreachable ("host is offline").
// Surface it to the browser as a build-time flag (inlined into import.meta.env);
// the embed path keys off its host fetcher instead (see isDatabricksWorkspace in
// host.ts). Keyed off the resolved mount so pointing at a bare workspace origin
// turns it on.
const isDatabricksWorkspaceEnv = parsed.pathname.replace(/\/$/, "") === WORKSPACE_API_PATH;
if (isDatabricksWorkspaceEnv) process.env.VITE_DATABRICKS_WORKSPACE = "true";

// Only serving uses the proxy, so a build never reads credentials or stops for a missing token.
function devProxyAuth(): Plugin {
  return {
    name: "dev-proxy-auth",
    apply: "serve",
    async config() {
      if (!useAuth) {
        console.log(`[dev-proxy] target=${OMNIGENT_URL}`);
        return;
      }
      // Serve only once a token is ready, so the first requests are authenticated.
      if (!process.env.OMNIGENT_AUTH_TOKEN) await refreshToken(parsed.origin);
      const token = resolveToken(parsed.origin);
      if (token) {
        console.log(
          `[dev-proxy] target=${OMNIGENT_URL} (authenticated${isDatabricksWorkspaceEnv ? ", databricks-workspace" : ""})`,
        );
        return;
      }
      const login = DATABRICKS_PROFILE
        ? `--profile ${DATABRICKS_PROFILE}`
        : `--host ${parsed.origin}`;
      console.error(
        `\n[dev-proxy] ERROR: No auth token for ${parsed.origin}.\n` +
          `  Set OMNIGENT_AUTH_TOKEN or OMNIGENT_DATABRICKS_PROFILE, or run:  databricks auth login ${login}\n`,
      );
      process.exit(1);
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

export default defineConfig(({ command, mode }) => ({
  // Relative asset base for production builds so the SPA can be served under
  // any path prefix (e.g. code-server's `/proxy/6767/`), decided purely at
  // server runtime via OMNIGENT_WEB_BASE_PATH — no separate build needed per
  // deployment. Dynamic code-split chunks and the Monaco worker then resolve
  // relative to `import.meta.url` instead of a hardcoded `/assets/...`. The
  // server rewrites the entry/asset refs in `index.html` to absolute
  // `{base}/assets/...` at serve time (see `_rewrite_web_ui_index` in
  // omnigent/server/app.py). Dev (`vite serve`) stays at root.
  base: command === "build" ? "./" : "/",
  plugins: [
    devProxyAuth(),
    safariLookbehindWorkarounds(),
    react(),
    tailwindcss(),
    ...devServerWorkerExports(command, mode).plugins,
  ],
  // Workers have no DOM, so they take a package's worker entry over its browser one.
  worker: workerBuildOptions,
  optimizeDeps: devServerWorkerExports(command, mode).optimizeDeps,
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
        // Storybook-only modules are covered by the pinned visual snapshot suite.
        "src/**/*.stories.{ts,tsx}",
        "src/storybook/**",
        "src/**/*storyFixtures.{ts,tsx}",
        "src/**/*StoryFixtures.{ts,tsx}",
        // Vendored UI kit, not product code (see tests/e2e_ui/COVERAGE_GAPS.md).
        "src/components/ai-elements/**",
        // Onboarding wizard pieces with no jsdom-testable logic: the WebGL2
        // shader + its canvas wrapper (no GL context in jsdom) and the Electron
        // entry (createRoot against the preload bridge). The flow's real logic
        // (steps, URL normalization) stays counted and is unit-tested.
        "src/components/onboarding/PixelBlast.tsx",
        "src/components/onboarding/AnimatedOmnigentPanel.tsx",
        "src/server-selector-v2.tsx",
      ],
      reportsDirectory: "./coverage",
      // text-summary: human-readable console line; json-summary: machine-
      // readable coverage/coverage-summary.json that CI distills to total.txt.
      reporter: ["text-summary", "json-summary"],
    },
  },
  server: {
    proxy: createProxyConfig(OMNIGENT_URL, useAuth),
  },
  build: {
    // default baseline is Safari 16.4+; iPadOS 15 can't parse dep regex lookbehinds (#1978)
    target: ["chrome111", "edge111", "firefox114", "safari15", "ios15"],
    outDir: path.resolve(__dirname, "../omnigent/server/static/web-ui"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: (id: string) => streamdownManualChunk(id) ?? shikiManualChunk(id),
      },
    },
  },
}));
