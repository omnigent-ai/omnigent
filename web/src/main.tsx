import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.tsx";
import { ThemeProvider } from "./components/theme/ThemeProvider";
import { TooltipProvider } from "./components/ui/tooltip";
import { ImageLightboxProvider } from "./components/ImageLightbox";
import { RunnerHealthProvider } from "./hooks/RunnerHealthProvider";
import { QueueFlushProvider } from "./hooks/QueueFlushProvider";
import { SessionUpdatesProvider } from "./hooks/SessionUpdatesProvider";
import { resolveServerInfo, type ServerInfo } from "./lib/capabilities";
import { CapabilitiesProvider } from "./lib/CapabilitiesContext";
import { createBootServerInfo, withBootTimeout } from "./lib/bootCapabilities";
import { isLoginRedirectPending, resolveIdentity } from "./lib/identity";
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
//
// Started here but AWAITED at the render gate below, alongside the
// /v1/info probe. The app's shell mounts ~8 queries (hosts, agents,
// conversations, projects, harnesses) with no auth gating of their own,
// so rendering before this settles fans them all out at once — logged
// out, every one 401s in parallel and the login redirect races the
// burst. Kicked off at module scope so it runs CONCURRENTLY with
// `resolveServerInfo()`; the gate waits on the slower of the two rather
// than chaining a second round-trip onto first paint.
const bootIdentity = resolveIdentity();

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
const bootServerInfo = createBootServerInfo(resolveServerInfo());

// Same 1.5s safety net for the identity probe: a hung /v1/me must not
// deadlock first paint. On timeout we render anyway and the app degrades
// exactly as it did before this gate existed.
const bootIdentityGate = withBootTimeout<string | null>(bootIdentity, null);

function RootApp({ initialInfo }: { initialInfo: ServerInfo }) {
  const [info, setInfo] = useState(initialInfo);
  useEffect(() => {
    let alive = true;
    void bootServerInfo.settled.then((resolved) => {
      if (alive) setInfo(resolved);
    });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    if (info.branding?.app_name) document.title = info.branding.app_name;
    const faviconUrl = info.branding?.logos.favicon;
    if (!faviconUrl) return;
    let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.removeAttribute("type");
    link.href = faviconUrl;
  }, [info]);
  return (
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
  );
}

void Promise.all([bootServerInfo.initial, bootIdentityGate]).then(([initialInfo]) => {
  // `/v1/me` came back 401 with a login page and we're already on our way
  // there. Mounting now would start a navigation race we lose either way:
  // the shell's queries fire against a session we know is invalid, 401,
  // and get torn down mid-flight when the login page commits. Header mode
  // never lands here (no login page), so a proxy-less deploy still renders.
  if (isLoginRedirectPending()) return;
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <RootApp initialInfo={initialInfo} />
    </StrictMode>,
  );
});
