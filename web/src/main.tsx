import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.tsx";
import { PWAUpdateBanner } from "./components/pwa/PWAUpdateBanner";
import { ThemeProvider } from "./components/theme/ThemeProvider";
import { TooltipProvider } from "./components/ui/tooltip";
import { ImageLightboxProvider } from "./components/ImageLightbox";
import { RunnerHealthProvider } from "./hooks/RunnerHealthProvider";
import { SessionUpdatesProvider } from "./hooks/SessionUpdatesProvider";
import { resolveServerInfo, type ServerInfo } from "./lib/capabilities";
import { CapabilitiesProvider } from "./lib/CapabilitiesContext";
import { resolveIdentity } from "./lib/identity";
import { initNativeInsets } from "./lib/nativeInsets";
import { initBrowserTelemetry } from "./lib/telemetry";
import {
  applyUiFontFamily,
  applyUiFontScale,
  readUiFontFamily,
  readUiFontSizePx,
} from "./lib/uiFontPreferences";
import { initChatStore } from "./store/chatStore";
import "./index.css";

// Start tracing before any request fires so fetch/XHR are patched in time
// and a trace begins in the browser. No-op unless a collector endpoint is
// configured (VITE_OTEL_EXPORTER_OTLP_ENDPOINT).
initBrowserTelemetry();

// Single client at module scope — shared across the whole app.
//
// `refetchOnWindowFocus: false` is intentional: window-focus auto-refetch
// is great for SaaS dashboards but noisy for chat. We can re-enable
// per-query later (e.g. the agents list, when we add it) by passing
// `refetchOnWindowFocus: true` on that specific `useQuery`.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
});

// Hand the QueryClient to the chat store so its actions can
// invalidate cached queries (e.g. the conversations list when a new
// conversation is created server-side).
initChatStore(queryClient);

// Discover the current user identity from the server. Once resolved,
// all subsequent fetch calls include X-Forwarded-Email so session
// routes know who's making the request.
void resolveIdentity();

// Mirror the iOS shell's native bar footprints into the inset CSS variables.
// No-op off the iOS shell (the inset vars stay at their env()-only defaults).
initNativeInsets();

// Apply the saved UI font size and family before first paint so there's no flash.
applyUiFontScale(readUiFontSizePx());
applyUiFontFamily(readUiFontFamily());

// Probe /v1/info BEFORE the first render so the route table knows
// whether to mount accounts routes. The probe is unauthed and the
// failure path resolves to "accounts off" — so even a stalled or
// missing server doesn't deadlock first paint. We add a small
// safety timeout (1.5s) so users on a flaky network still get
// something on screen.
const _bootProbe: Promise<ServerInfo> = Promise.race([
  resolveServerInfo(),
  new Promise<ServerInfo>((resolve) =>
    setTimeout(
      () =>
        resolve({
          accounts_enabled: false,
          login_url: null,
          needs_setup: false,
          databricks_features: false,
          managed_sandboxes_enabled: false,
          sandbox_provider: null,
          server_version: null,
          smart_routing_enabled: false,
        }),
      1500,
    ),
  ),
]);

void _bootProbe.then((info) => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <CapabilitiesProvider info={info}>
        <QueryClientProvider client={queryClient}>
          <ThemeProvider>
            <PWAUpdateBanner />
            <TooltipProvider>
              <ImageLightboxProvider>
                <BrowserRouter>
                  <SessionUpdatesProvider>
                    <RunnerHealthProvider>
                      <App />
                    </RunnerHealthProvider>
                  </SessionUpdatesProvider>
                </BrowserRouter>
              </ImageLightboxProvider>
            </TooltipProvider>
          </ThemeProvider>
        </QueryClientProvider>
      </CapabilitiesProvider>
    </StrictMode>,
  );
});
