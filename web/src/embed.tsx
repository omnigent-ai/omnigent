// Embed entry point.
//
// Exposes `OmnigentApp` — a plain React component (app-specific providers +
// routes, NO React root, NO router) — for a host (e.g. the Databricks monolith)
// to render the full web experience directly inside its own React tree and
// its own router. web's `<Routes>` match the absolute host pathname (route
// paths are prefixed with `basename`) and its routing hooks (useNavigate/Link)
// rebase absolute targets under `basename`. This is the same-root path.
//
// The embed:
//   - injects the host transport config (API fetcher + WebSocket URL),
//   - tags a root element with `.omnigent-app` so the scoped stylesheet
//     applies and Radix overlays portal back into this subtree,
//   - applies the host-provided color scheme (`isDarkMode`): the embed is not
//     user-toggleable (the theme switcher is hidden via `useIsEmbedded`); the
//     host drives light/dark and the embed mirrors it,
//   - owns its OWN QueryClient (react-query is bundled, not shared).
//
// Only React/ReactDOM, react/jsx-runtime, and react-router(-dom) are left as
// BARE externals in the intermediate build (`vite.embed.config.ts`); the host
// monolith's rspack resolves them to its own copies (React 18, react-router
// 6.4.1), so the island shares the host's single React + react-router instance.
// Everything else (react-query, monaco, shiki, xterm, …) is bundled by Vite;
// rspack then ingests the single intermediate module and emits the final chunks.

import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ThemeProvider as NextThemesProvider } from "next-themes";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import App from "./App";
import { TooltipProvider } from "./components/ui/tooltip";
import { ImageLightboxProvider } from "./components/ImageLightbox";
import { RunnerHealthProvider } from "./hooks/RunnerHealthProvider";
import { CapabilitiesContext } from "./lib/CapabilitiesContext";
import { resolveServerInfo, type ServerInfo } from "./lib/capabilities";
import { EmbeddedProvider } from "./lib/embedded";
import { type OmnigentHostConfig, setEmbedRoot, setOmnigentHostConfig } from "./lib/host";
import { resolveIdentity } from "./lib/identity";
import {
  type RoutingApi,
  RoutingProvider,
  basenamedRouting,
  reactRouterRouting,
} from "./lib/routing";
import { initChatStore } from "./store/chatStore";
import "./index.css";
import { QueueFlushProvider } from "./hooks/QueueFlushProvider";
import { SessionUpdatesProvider } from "./hooks/SessionUpdatesProvider";

export type { OmnigentHostConfig } from "./lib/host";
export type { RoutingApi } from "./lib/routing";

// Re-export the host-config setter so the host can install transport config
// EAGERLY (before first render), independent of React render/prop timing. The
// config is a module-level singleton in `host.ts`, shared across all chunks.
export { setOmnigentHostConfig } from "./lib/host";

// The embed owns its QueryClient (react-query is bundled, not shared with the
// host). One client at module scope, shared across the whole embed — mirrors
// `main.tsx`'s standalone client (chat-tuned: 30s stale, no window-focus
// refetch, which is noisy for chat).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
});

export interface OmnigentAppProps extends OmnigentHostConfig {
  /**
   * Router basename, e.g. `/ml/omnigent-embed`. web's routes + navigation
   * use absolute paths (`/`, `/c/:conversationId`), so the app must be nested
   * under the host mount path.
   *
   * No nested `<Router>` is rendered; route matching is relative (descendant
   * routes), and `navigate()`/`<Link to>` are rebased under `basename` via
   * `basenamedRouting`.
   */
  basename?: string;
  /**
   * Optional routing inversion-of-control override. Merged over the default
   * react-router-dom implementation. In the same-root path react-router-dom is
   * already the host's instance (externalized), so this is rarely needed.
   */
  routing?: Partial<RoutingApi>;
  /**
   * Host-provided color scheme. The embed has no theme switcher of its own
   * (see `useIsEmbedded`); the host owns the theme and passes it here (e.g.
   * `theme.isDarkMode` from `useDesignSystemTheme`). Defaults to light.
   */
  isDarkMode?: boolean;
}

/**
 * Shared provider stack + `<App/>` route table. Expects a router context to
 * already be present — the host (universe) supplies it; the embed renders in
 * the host's same React tree (shared react-router). The embed brings its OWN
 * `<QueryClientProvider>` (react-query is bundled, not shared). Self-contained
 * for styling: renders its own `.omnigent-app` scope wrapper and registers it
 * as the Radix portal root, so the host only renders this — no class/portal
 * wiring needed.
 */
// Sentinel used when the `/v1/info` probe is slow or missing — matches
// `main.tsx`'s fallback (accounts off, no login).
const SERVER_INFO_OFFLINE_FALLBACK: ServerInfo = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: false,
};

/**
 * Runs `main.tsx`'s boot-time `/v1/info` probe inside the embed tree.
 *
 * Standalone resolves the probe BEFORE its first render and renders the whole
 * app under `<CapabilitiesProvider>`. The embed is rendered synchronously by
 * the host, so there's no "await then render" seam — instead we hold the
 * context at the default `"loading"` and flip it once the probe resolves.
 *
 * WITHOUT this provider, `useServerInfo()` returns the context default
 * (`"loading"`) forever, so `App` hits its `if (info === "loading") return
 * null` guard and the embed renders a permanently blank `.omnigent-app` div.
 */
function EmbedCapabilitiesProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<ServerInfo | "loading">("loading");
  useEffect(() => {
    let alive = true;
    // Fail open to "accounts off" on a slow/missing probe (same 1.5s budget as
    // main.tsx) so the chat UI still paints instead of hanging on "loading".
    void Promise.race([
      resolveServerInfo(),
      new Promise<ServerInfo>((resolve) =>
        setTimeout(() => resolve(SERVER_INFO_OFFLINE_FALLBACK), 1500),
      ),
    ]).then((resolved) => {
      if (alive) setInfo(resolved);
    });
    return () => {
      alive = false;
    };
  }, []);
  return <CapabilitiesContext.Provider value={info}>{children}</CapabilitiesContext.Provider>;
}

function OmnigentProviders({
  routing,
  basename,
  isDarkMode,
}: {
  routing: RoutingApi;
  basename?: string;
  isDarkMode?: boolean;
}) {
  // Reuse the host's QueryClient (read from the ambient provider) and run the
  // one-time side effects (wire the chat store to that client + resolve
  // identity). `initChatStore` only stashes the client for later cache
  // invalidation, so doing this in a mount-once initializer is fine.
  const queryClient = useQueryClient();
  useState(() => {
    initChatStore(queryClient);
    void resolveIdentity();
    return null;
  });

  // Register the theme wrapper as the Radix portal container so overlays land
  // inside the themed subtree (and clear it on unmount). It's the inner div —
  // not the `.omnigent-app` scope root — so portaled overlays inherit the
  // `.dark` token overrides too.
  const scopeRef = useCallback((el: HTMLDivElement | null) => {
    setEmbedRoot(el);
  }, []);

  return (
    // Two nested wrappers on purpose:
    //   - `.omnigent-app` (outer) is the scope anchor. The scoped stylesheet
    //     rewrites `:root` → `.omnigent-app` (light tokens) and `.dark` →
    //     `.omnigent-app .dark`, so the dark class must be a DESCENDANT of the
    //     scope root, not the root itself.
    //   - the inner div carries the host-driven `dark` class (when dark) and is
    //     the Radix portal root, so both the app and its overlays read the dark
    //     token overrides. Light mode = no class → inherits the scope root's
    //     light tokens.
    <div className="omnigent-app" style={{ height: "100%", width: "100%" }}>
      <div
        ref={scopeRef}
        className={isDarkMode ? "dark" : undefined}
        style={{ height: "100%", width: "100%" }}
      >
        <EmbeddedProvider>
          {/* next-themes is kept as the JS source of truth for `resolvedTheme`
              (Monaco + the xterm terminal read it via `useTheme()`); the host
              drives the value via `forcedTheme`. A private attribute +
              `enableColorScheme={false}` keep it from mutating the host's
              `<html>` class or `color-scheme`. */}
          <NextThemesProvider
            attribute="data-omnigent-theme"
            forcedTheme={isDarkMode ? "dark" : "light"}
            enableColorScheme={false}
            disableTransitionOnChange
          >
            <TooltipProvider>
              <ImageLightboxProvider>
                <RoutingProvider value={routing}>
                  <EmbedCapabilitiesProvider>
                    <SessionUpdatesProvider>
                      <RunnerHealthProvider>
                        <QueueFlushProvider>
                          <App basename={basename} />
                        </QueueFlushProvider>
                      </RunnerHealthProvider>
                    </SessionUpdatesProvider>
                  </EmbedCapabilitiesProvider>
                </RoutingProvider>
              </ImageLightboxProvider>
            </TooltipProvider>
          </NextThemesProvider>
        </EmbeddedProvider>
      </div>
    </div>
  );
}

/**
 * The Omnigent app for the SAME-ROOT path: a plain component the host renders
 * directly inside its OWN React tree + router. Renders NO `<Router>` (that
 * would throw "Router inside a Router").
 *
 * Routing under `basename` is handled on BOTH sides:
 *   - route MATCHING: `App` declares absolute paths (`${basename}/c/:id`) so it
 *     matches the full host pathname directly — no reliance on the host's
 *     route-match context (whose react-router instance may differ from ours).
 *   - NAVIGATION/links: `navigate()`/`<Link to>` absolute targets are rebased
 *     under `basename` via `basenamedRouting` (the routing IoC).
 */
export function OmnigentApp({
  basename,
  routing,
  isDarkMode,
  ...hostConfig
}: OmnigentAppProps = {}) {
  // Install transport config ONCE per mount (not on every render). Setting it in
  // the render body re-ran on every (re)render, and concurrent/Suspense renders
  // could re-invoke with empty props — clobbering the good config with `{}`.
  // The host also sets this eagerly at load (see `loadOmnigentApp`); this is a
  // belt-and-suspenders that captures the first non-empty props.
  useState(() => {
    setOmnigentHostConfig(hostConfig);
    return null;
  });

  const routingApi = useMemo<RoutingApi>(() => {
    // Host overrides compose UNDER the basename wrapper: merge the host's
    // primitives over the react-router defaults FIRST, then wrap the result in
    // `basenamedRouting` so navigate()/<Link> rebasing still applies to the
    // host's implementations. (Merging AFTER `basenamedRouting` — `{ ...base,
    // ...routing }` — would clobber its rebased navigate/Link with the host's
    // un-rebased ones, so absolute targets would land at the host root instead
    // of under the mount path.)
    const merged: RoutingApi = { ...reactRouterRouting, ...routing };
    return basename ? basenamedRouting(basename, merged) : merged;
  }, [basename, routing]);

  // The embed owns its QueryClient (bundled react-query); `OmnigentProviders`
  // reads it back via `useQueryClient()` under this provider.
  return (
    <QueryClientProvider client={queryClient}>
      <OmnigentProviders routing={routingApi} basename={basename} isDarkMode={isDarkMode} />
    </QueryClientProvider>
  );
}
