// Standalone entry for the gated server selector v2 (Electron shell).
//
// Loaded from a file:// window when OMNIGENT_SERVER_SELECTOR_V2=1 (see
// electron/src/main.js `setupPagePath`). Renders the ServerSelectorV2, wiring it
// to the shell's `omnigentSetup` preload bridge (server URL, recent / managed
// servers, connect, start-local). Theme follows the OS via index.css.

import { type CSSProperties, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ServerSelectorV2, type ServerSelectorV2Setup } from "./pages/onboarding/ServerSelectorV2";
import { maybeMockSetup } from "./pages/onboarding/mockSetup";
import "./index.css";

const DEFAULT_URL = "http://localhost:6767";
const CLOUD_DOCS_URL = "https://omnigent.ai/docs/deploy/overview";

/** The `omnigentSetup` preload bridge (see electron/src/preload.js). */
interface OmnigentSetup {
  getServerUrl: () => Promise<string | null>;
  setServerUrl: (url: string) => Promise<unknown>;
  getManagedServers: () => Promise<string[]>;
  getRecentServers: () => Promise<string[]>;
  forgetRecentServer?: (url: string) => Promise<string[]>;
  checkServer?: (url: string) => Promise<{ status: "ok" | "reachable" | "unreachable" }>;
  copyText: (text: string) => Promise<unknown>;
  setServerSelectorV2?: (enabled: boolean) => Promise<unknown>;
  getCliStatus: () => Promise<{ installed?: boolean; installSupported?: boolean }>;
  startLocalServer: () => Promise<{ ok?: boolean; url?: string; error?: string }>;
  onLocalServerSetupLog?: (cb: (line: string) => void) => () => void;
  installCli?: () => Promise<{ ok?: boolean; error?: string; installed?: boolean }>;
  onCliInstallLog?: (cb: (line: string) => void) => () => void;
}

function setupBridge(): OmnigentSetup | undefined {
  return (window as unknown as { omnigentSetup?: OmnigentSetup }).omnigentSetup;
}

// Mock-or-real router: with `?mock=1` render the URL-param mock (dev only, see
// mockSetup.ts), otherwise the real bridge-wired flow. Split so each branch's
// hooks run unconditionally (rules of hooks).
function SetupApp() {
  const mock = maybeMockSetup(new URLSearchParams(window.location.search));
  if (mock) {
    return <ServerSelectorV2 setup={mock} />;
  }
  return <BridgeSetupApp />;
}

function BridgeSetupApp() {
  const params = new URLSearchParams(window.location.search);
  const failedUrl = params.get("url");
  const error = params.get("error") ?? undefined;
  const isEphemeral = params.get("ephemeral") === "1";
  // "Connect to new server…" opens straight on the server list (?step=server),
  // skipping the first-run landing/mode intro.
  const initialStep = params.get("step") === "server" ? ("server" as const) : undefined;

  // Prefill: the URL that just failed (retry is the common next step), else the
  // saved server, else the default — except in ephemeral mode, where the whole
  // point is connecting to a *different* server than the saved one.
  const [initialUrl, setInitialUrl] = useState(failedUrl ?? DEFAULT_URL);
  const [recentServers, setRecentServers] = useState<string[]>([]);
  const [managedServers, setManagedServers] = useState<string[]>([]);
  // Whether the `omnigent` CLI is installed — decides "Install" vs "Open" and
  // the returning-user start step. Undefined until the probe resolves.
  const [installed, setInstalled] = useState<boolean | undefined>(undefined);
  // Whether in-app install is available on this platform (macOS only). Off →
  // connect/local must never route through an install step.
  const [installSupported, setInstallSupported] = useState(false);
  // Hold the initial paint until the CLI probe resolves, so the wizard opens on
  // the correct step (welcome vs server list) instead of flashing the wrong one.
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const bridge = setupBridge();
    // No shell bridge (browser preview): nothing to probe, render immediately.
    if (!bridge) {
      setReady(true);
      return;
    }
    // Load ALL setup data before the first paint — not just CLI status. The
    // initial step depends on recents/managed too (a returning user starts on
    // the server list), so mounting before those resolve would open the empty
    // "add" view and never switch when the lists arrive. allSettled: a single
    // failed probe degrades to its default, never blocks the wizard.
    const savedUrl =
      !failedUrl && !isEphemeral
        ? bridge.getServerUrl().then((saved) => setInitialUrl(saved || DEFAULT_URL))
        : Promise.resolve();
    const recents = bridge.getRecentServers().then(setRecentServers);
    const managed = bridge.getManagedServers().then(setManagedServers);
    const cli = bridge.getCliStatus().then((status) => {
      setInstalled(status?.installed === true);
      setInstallSupported(status?.installSupported === true);
    });
    Promise.allSettled([savedUrl, recents, managed, cli]).then(() => {
      // A failed CLI probe means "not installed" rather than unknown.
      setInstalled((prev) => prev ?? false);
      setReady(true);
    });
  }, [failedUrl, isEphemeral]);

  const setup: ServerSelectorV2Setup = {
    initialUrl,
    initialStep,
    error,
    recentServers,
    managedServers,
    installed,
    onConnect: async (url) => {
      // setServerUrl persists the URL and navigates the window to it; on success
      // the server's SPA takes over and this page goes away. A rejection (e.g.
      // main-side normalizeUrl rejects an input the renderer accepted) is
      // surfaced as {error} so the step can show it, rather than a click that
      // silently does nothing.
      const bridge = setupBridge();
      if (!bridge) return { error: "The desktop shell is unavailable." };
      try {
        await bridge.setServerUrl(url);
        return {};
      } catch (e) {
        return { error: e instanceof Error ? e.message : "Could not connect to that server." };
      }
    },
    onStartLocal: async () => {
      // Start (or reuse) the local server, then navigate to it. Resolves the
      // outcome so the terminal step can show ready/failed. On success the
      // setServerUrl navigation replaces this page, so this never resolves in
      // the happy path — the terminal stays on "Ready" until the window swaps.
      const bridge = setupBridge();
      if (!bridge) return { ok: false, error: "The desktop shell is unavailable." };
      try {
        const result = await bridge.startLocalServer();
        if (result?.ok && result.url) {
          await bridge.setServerUrl(result.url);
          return { ok: true };
        }
        return { ok: false, error: result?.error ?? "Could not start the local server." };
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
    // Live startup-log stream from the shell, if this shell exposes it (older
    // shells / browser preview omit it → the terminal step shows phases only).
    onSetupLog: setupBridge()?.onLocalServerSetupLog
      ? (cb) => setupBridge()?.onLocalServerSetupLog?.(cb) ?? (() => {})
      : undefined,
    // Install the CLI, if the shell supports it. Offered only when NOT already
    // installed — a returning user opens rather than installs.
    onInstallCli:
      installed === false && installSupported && setupBridge()?.installCli
        ? async () => {
            const bridge = setupBridge();
            if (!bridge?.installCli) return { ok: false, error: "Install is unavailable." };
            try {
              const result = await bridge.installCli();
              setInstalled(result?.installed === true);
              // The installer can exit 0 yet leave the binary unresolvable (PATH
              // issue). Treat "installed:false after a successful run" as a
              // failure rather than proceeding to start/connect with no CLI.
              if (result?.ok && result.installed !== true) {
                return {
                  ok: false,
                  error:
                    "The installer finished but the omnigent CLI still isn't detected. " +
                    "Try opening a new terminal, or install it from https://omnigent.ai/.",
                };
              }
              return { ok: result?.ok === true, error: result?.error };
            } catch (e) {
              return { ok: false, error: e instanceof Error ? e.message : String(e) };
            }
          }
        : undefined,
    onInstallLog: setupBridge()?.onCliInstallLog
      ? (cb) => setupBridge()?.onCliInstallLog?.(cb) ?? (() => {})
      : undefined,
    // Only offered when the shell exposes the forget method (newer shells).
    onRemoveServer: setupBridge()?.forgetRecentServer
      ? (url) => {
          // Optimistic: drop it locally, then reconcile with the shell's result.
          setRecentServers((prev) => prev.filter((u) => u !== url));
          setupBridge()
            ?.forgetRecentServer?.(url)
            .then(setRecentServers)
            .catch(() => {});
        }
      : undefined,
    // Advisory reachability probe for a just-added server. Resolves a status;
    // never blocks Join. Absent bridge (browser preview) → treat as unreachable.
    onCheckServer: async (url) => {
      const bridge = setupBridge();
      if (!bridge?.checkServer) return { status: "unreachable" as const };
      try {
        return await bridge.checkServer(url);
      } catch {
        return { status: "unreachable" as const };
      }
    },
    // Copy via the shell's native clipboard bridge — navigator.clipboard is
    // denied on the file:// wizard page.
    onCopy: (text) => {
      setupBridge()
        ?.copyText(text)
        .catch(() => {});
    },
    // Cloud deploy docs open in the real browser: window.open on this file://
    // page is routed out by the shell's popup policy, not opened in-window.
    onCloudSetup: () => window.open(CLOUD_DOCS_URL, "_blank", "noopener"),
    // Revert to the classic setup page; the shell persists it and reloads.
    onSwitchToLegacy: () => {
      setupBridge()
        ?.setServerSelectorV2?.(false)
        ?.catch(() => {});
    },
  };

  return (
    <>
      {/* Window drag surface: with the native title bar hidden (titleBarStyle
          "hiddenInset" / frame:false, see electron/src/main.js) this strip is
          the only place the user can grab to move the window. Matches the
          static setup page's 36px .drag-strip. */}
      <div
        style={
          {
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            height: 36,
            WebkitAppRegion: "drag",
          } as CSSProperties
        }
      />
      {ready ? (
        <ServerSelectorV2 setup={setup} />
      ) : (
        // Hold on the wizard background until the CLI probe resolves, so the
        // flow opens on the correct step rather than flashing the wrong one.
        <div
          className="min-h-screen"
          style={{ background: "var(--onboarding-wizard-background)" }}
        />
      )}
    </>
  );
}

const container = document.getElementById("server-selector-v2-root");
if (container) createRoot(container).render(<SetupApp />);
