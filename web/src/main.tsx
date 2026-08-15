import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.tsx";
import { ThemeProvider } from "./components/theme/ThemeProvider";
import { TooltipProvider } from "./components/ui/tooltip";
import { ImageLightboxProvider } from "./components/ImageLightbox";
import { RunnerHealthProvider } from "./hooks/RunnerHealthProvider";
import { QueueFlushProvider } from "./hooks/QueueFlushProvider";
import { SessionUpdatesProvider } from "./hooks/SessionUpdatesProvider";
import { FALLBACK_SERVER_INFO, resolveServerInfo, type ServerInfo } from "./lib/capabilities";
import { CapabilitiesProvider } from "./lib/CapabilitiesContext";
import { resolveIdentity } from "./lib/identity";
import { initNativeInsets } from "./lib/nativeInsets";
import { initBrowserTelemetry } from "./lib/telemetry";
import {
  applyDesktopUiFontSize,
  applyUiFontFamily,
  readUiFontFamily,
  readUiFontSizePx,
} from "./lib/uiFontPreferences";
import { applyThemePalette, readThemePalette } from "./lib/themePalette";
import { applyCustomTheme, readCustomTheme } from "./lib/customTheme";
import { initChatStore } from "./store/chatStore";
import "katex/dist/katex.min.css";
import "streamdown/styles.css";
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

// Apply the saved desktop UI font size and family before first paint so there's no flash.
applyDesktopUiFontSize(readUiFontSizePx());
applyUiFontFamily(readUiFontFamily());

// The standalone sidebar font size control was removed. Clear its legacy value
// so sidebar items follow the shared desktop interface size.
if (typeof window !== "undefined") {
  try {
    localStorage.removeItem("omnigent:sidebar-font-size");
  } catch {
    // localStorage access errors are non-fatal.
  }
}

// Apply the saved color palette (data-theme on <html>) before first paint too,
// so the app renders in the chosen theme rather than flashing the brand default.
applyCustomTheme(readCustomTheme());
applyThemePalette(readThemePalette());

// Probe /v1/info BEFORE the first render so the route table knows
// whether to mount accounts routes. The probe is unauthed and the
// failure path resolves to "accounts off" — so even a stalled or
// missing server doesn't deadlock first paint. We add a small
// safety timeout (1.5s) so users on a flaky network still get
// something on screen.
const bootProbe: Promise<ServerInfo> = Promise.race([
  resolveServerInfo(),
  new Promise<ServerInfo>((resolve) => {
    setTimeout(() => resolve(FALLBACK_SERVER_INFO), 1500);
  }),
]);

const root = createRoot(document.getElementById("root")!);
const renderApp = (info: ServerInfo) => {
  root.render(
    <StrictMode>
      <CapabilitiesProvider info={info}>
        <QueryClientProvider client={queryClient}>
          <ThemeProvider>
            <TooltipProvider>
              <ImageLightboxProvider>
                <BrowserRouter>
                  <SessionUpdatesProvider>
                    <RunnerHealthProvider>
                      <QueueFlushProvider>
                        <App />
                      </QueueFlushProvider>
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
};

// Paint as soon as the boot probe settles — the real value, or the 1.5s
// fallback on a slow/missing probe.
void bootProbe.then(renderApp);
// Then settle on the real value once it lands. If the 1.5s fallback painted
// first (slow-but-successful probe), this adopts the real /v1/info and
// re-renders the same root — so capability-gated UI (e.g. the managed-sandbox
// host option, accounts routes) isn't pinned off for the tab's lifetime.
// resolveServerInfo caches, so this shares the boot probe's single fetch, and
// the real value always resolves no earlier than the fallback, so it never
// downgrades a real render back to the fallback.
void resolveServerInfo().then(renderApp);
