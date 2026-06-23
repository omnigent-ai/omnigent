import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { BrowserRouter } from "react-router-dom";
import App from "./App.tsx";
import i18n from "./i18n";
import { ThemeProvider } from "./components/theme/ThemeProvider";
import { TooltipProvider } from "./components/ui/tooltip";
import { RunnerHealthProvider } from "./hooks/RunnerHealthProvider";
import { SessionUpdatesProvider } from "./hooks/SessionUpdatesProvider";
import { resolveServerInfo, type ServerInfo } from "./lib/capabilities";
import { CapabilitiesProvider } from "./lib/CapabilitiesContext";
import { resolveIdentity } from "./lib/identity";
import { initChatStore } from "./store/chatStore";
import "./index.css";

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
        }),
      1500,
    ),
  ),
]);

void _bootProbe.then((info) => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <CapabilitiesProvider info={info}>
        <I18nextProvider i18n={i18n}>
          <QueryClientProvider client={queryClient}>
            <ThemeProvider>
              <TooltipProvider>
                <BrowserRouter>
                  <SessionUpdatesProvider>
                    <RunnerHealthProvider>
                      <App />
                    </RunnerHealthProvider>
                  </SessionUpdatesProvider>
                </BrowserRouter>
              </TooltipProvider>
            </ThemeProvider>
          </QueryClientProvider>
        </I18nextProvider>
      </CapabilitiesProvider>
    </StrictMode>,
  );
});
