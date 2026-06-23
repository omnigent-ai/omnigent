# Web UI base-path support (issue #1031)

**Date:** 2026-06-23
**Issue:** [#1031](https://github.com/omnigent-ai/omnigent/issues/1031) — Support serving the standalone Web UI under a subpath, e.g. code-server `/proxy/<port>/`.

## Problem

The standalone Omnigent Web UI assumes it is served from the origin root (`/`).
When a reverse proxy exposes the server under a path prefix — most notably
code-server's port proxy at `/proxy/<port>/` or `/absproxy/<port>/` — the SPA
loads but its API, SSE, and WebSocket calls target root-relative paths
(`/v1/...`), which the browser resolves against the proxy host root instead of
the prefixed path. Session loading, live streams, and terminal attach all fail.
Static assets (especially the ~700 lazily-loaded code-split chunks) also fail to
resolve under a prefix.

## code-server proxy semantics (the two modes we must support)

- **`/proxy/<port>/` — strips the prefix.** The backend receives `/v1/...`.
  The app must emit *prefixed* URLs from the browser (so code-server routes to
  the right port) but the server itself sees root-relative paths.
- **`/absproxy/<port>/` — does NOT strip.** The backend receives
  `/absproxy/<port>/v1/...`. The app must be configured with its base path AND
  the server must accept the prefixed paths.

A generic reverse proxy (nginx/Traefik `location /omnigent/ { proxy_pass ... }`
without path rewriting) behaves like `/absproxy/`.

## Goal

A single configuration value makes the whole app work behind any of these:

```
omnigent server --base-path /proxy/6767
OMNIGENT_WEB_BASE_PATH=/proxy/6767 omnigent server
```

Acceptance criteria (from the issue):
- Opening Omnigent at `/proxy/6767/` works in code-server.
- Session list and chat load; SSE streams connect; terminal WebSocket attaches.
- Static assets load without the app living at `/`.
- Root deployment at `http://localhost:6767/` is unchanged.

## Design

One config value (`base_path`), normalized to a leading-slash / no-trailing-slash
string (`/proxy/6767`); empty default preserves today's root behavior exactly.

### 1. Frontend — apply the base at every network/navigation seam

`ap-web` already funnels **all** HTTP through `hostFetch()` and **all**
WebSockets through `resolveWebSocketUrl()` (`src/lib/host.ts`). New module
`src/lib/basePath.ts`:

```ts
getBasePath(): string          // "" or "/proxy/6767", read once from window.__OMNIGENT_BASE_PATH__
withBasePath(path): string     // prepend base to an app-absolute path, idempotent
stripBasePath(pathname): string // remove base prefix for in-app path comparisons
```

`window.__OMNIGENT_BASE_PATH__` is injected by the server into `index.html`
(absent in dev / root → `getBasePath()` returns `""`).

Apply points:
- `host.ts` `hostFetch` (standalone branch only — embed's host fetcher is left
  untouched) and `resolveWebSocketUrl` prepend the base. This covers the REST
  API, SSE session stream (fetch-stream), terminal-attach WS, session-updates WS.
- `accountsApi.ts` `/auth/*` calls and `lastAssistantText.ts` use plain `fetch`
  (bypassing the choke point) → wrap their paths with `withBasePath`.
- `main.tsx`: `<BrowserRouter basename={getBasePath() || undefined}>`. react-router
  then natively rebases every in-app `<Link>` / `navigate()` / `useHref`.
- `identity.ts`: `_isOnLoginPath()` compares `stripBasePath(pathname)`; login
  redirects use `withBasePath(login_url)`.
- Full-page redirects that bypass react-router (`window.location.href = "/login"`
  etc. in `AccountMenu`, `RegisterPage`, `SetupPage`, `LoginPage` return-to
  default) → `withBasePath`.
- Manual absolute-URL builders: share link (`PermissionsModal`), invite link
  (`MembersPage`), and `getCliServerUrl()` include the base.

### 2. Assets — Vite relative base + server HTML rewrite

The app is heavily code-split (~700 chunks). With `base: "/"` (absolute) Vite
bakes `/assets/...` into dynamic-chunk URLs, which break under a prefix. Switch
the **build** to `base: "./"` (relative; dev stays `/`) so dynamic chunks and the
Monaco worker resolve relative to `import.meta.url` — correct under any prefix.

Relative `./assets/...` in `index.html` would break on deep-link refresh, so the
**server rewrites `index.html` once at startup**: `src="./` / `href="./` →
`src="{base}/` / `href="{base}/` (absolute), and injects the
`window.__OMNIGENT_BASE_PATH__` script. With the default empty base this yields
absolute `/assets/...` — identical to today's output, so the root deployment is
unchanged. No `<base href>` tag is used (avoids the SVG `url(#id)` fragment
gotcha).

### 3. Server — config + base-path-strip ASGI middleware

- **Config:** `base_path` flows from `--base-path` (CLI) / `OMNIGENT_WEB_BASE_PATH`
  (env) into `create_app()`. Normalized centrally.
- **`BasePathMiddleware`** (outermost ASGI layer): if an incoming `http`/`websocket`
  path equals or starts with `{base}`, strip the prefix and set `scope["root_path"]`.
  This is the single piece that unifies both proxy modes from one config value:
  - `/proxy/` (strip) → server already sees `/v1/...` → middleware no-ops.
  - `/absproxy/` or generic non-stripping proxy → server sees `/proxy/6767/v1/...`
    → middleware strips → normal `/v1` routing.
  Routing stays defined at `/v1` regardless; no per-router prefixing.
- **`index.html` serving** (`_SPAStaticFiles`): serve the cached, base-rewritten
  HTML for `index.html` / SPA-fallback responses.

### 4. CLI / docs

- `--base-path` option on the `omnigent server` command; folds into
  `OMNIGENT_WEB_BASE_PATH` consumed by `create_app()`.
- Document the code-server subpath setup in `deploy/` docs.

## Testing

- **Frontend (Vitest):** `basePath.test.ts` (normalize/with/strip incl. idempotency,
  trailing slash, empty); `host.test.ts` (fetch + ws prefixing when base set, no-op
  when empty, embed fetcher untouched); identity login-path check under base.
- **Python (pytest):** `BasePathMiddleware` strips/no-ops/sets root_path for http +
  websocket; `index.html` rewrite injects base + rewrites asset refs; empty base
  leaves output byte-identical; assets/API reachable at both `/v1/...` and
  `/{base}/v1/...` when base is set.

## Out of scope / follow-ups

- Full OIDC-behind-subpath (IdP `redirect_uri` must be the public prefixed URL) is
  governed by `OMNIGENT_ACCOUNTS_BASE_URL`; documented, not re-plumbed here.
- Dev server (`vite`) base-path serving — dev runs at root; the existing
  `OMNIGENT_URL` dev-proxy path handling is unchanged.

## Non-goals

- No change to runner↔server / host↔server URL construction (those already parse
  a base path from the server URL and connect directly, not through the proxy).
