// Dev-only mock of the Electron `omnigentSetup` bridge, so the four desktop
// onboarding variants (new/returning × MDM/no-MDM) are reachable in a plain
// `vite dev` browser — no Electron, no macOS Managed Preferences, no `defaults`.
//
// Kept entirely separate from the real bridge wiring in server-selector-v2.tsx:
// that file calls maybeMockSetup() once and, if it returns a setup, skips the
// bridge path completely. Never active in production — gated on `?mock=1`, which
// the packaged shell never appends.
//
// Query params:
//   mock=1                        enable the mock (required)
//   managed=<url>,<url>           MDM-preset servers (comma-separated)
//   recents=<url>,<url>           recent servers (comma-separated)
//   installed=1                   returning user (omnigent CLI already installed)
//   step=server                   open straight on the server list
//   error=<msg>                   show a connect-error banner
//
// Examples (all against the vite dev server):
//   new + no MDM ............ ?mock=1
//   returning + no MDM ...... ?mock=1&installed=1&recents=https://old.example.com
//   new + MDM ............... ?mock=1&managed=https://field-eng.example.com,https://corp.example.com
//   returning + MDM ......... ?mock=1&installed=1&managed=https://field-eng.example.com

import type { ServerSelectorV2Setup } from "./ServerSelectorV2";

/** Whether the mock is requested (`?mock=1`). */
export function isMockSetup(params: URLSearchParams): boolean {
  return params.get("mock") === "1";
}

/** Split a comma-separated param into trimmed, non-empty URLs. */
function urlList(params: URLSearchParams, key: string): string[] {
  return (params.get(key) ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Build a fully-stubbed {@link ServerSelectorV2Setup} from the URL params, or
 * null when the mock isn't requested. Connect/start/copy actions are no-op
 * stubs that log to the console instead of touching a real server.
 *
 * @param params The page's URL search params.
 * @returns A mock setup to render, or null to fall through to the real bridge.
 */
export function maybeMockSetup(params: URLSearchParams): ServerSelectorV2Setup | null {
  if (!isMockSetup(params)) return null;

  const managedServers = urlList(params, "managed");
  const recentServers = urlList(params, "recents");
  const installed = params.get("installed") === "1";
  const error = params.get("error") ?? undefined;
  const initialStep = params.get("step") === "server" ? ("server" as const) : undefined;

  const log = (action: string, detail?: unknown) =>
    console.info(`[onboarding mock] ${action}`, detail ?? "");

  // The mock installer streams its output only once it starts — mirroring the
  // real adapter, whose lines appear after install begins, not on subscribe.
  // onInstallLog registers the sink; onInstallCli emits into it and resolves
  // when the stream finishes.
  let installLog: ((line: string) => void) | null = null;

  return {
    initialUrl: recentServers[0] ?? managedServers[0] ?? "http://localhost:6767",
    initialStep,
    error,
    recentServers,
    managedServers,
    installed,
    mockInstall: true,
    onConnect: async (url) => {
      log("onConnect", url);
      return {};
    },
    onStartLocal: async () => {
      log("onStartLocal");
      return { ok: true };
    },
    onInstallCli: () => {
      log("onInstallCli");
      // Stream the install lines only once install starts, resolving when the
      // stream finishes — so the warm-up beat shows an empty terminal first.
      return new Promise<{ ok: boolean; error?: string }>((resolve) => {
        const lines = [
          "Installing uv (required by the Omnigent installer)…",
          "Installing the Omnigent CLI…",
          "uv tool install --force --python 3.12 omnigent",
          "Installed omnigent",
        ];
        let i = 0;
        const timer = setInterval(() => {
          if (i < lines.length) installLog?.(lines[i++]);
          else {
            clearInterval(timer);
            resolve({ ok: true });
          }
        }, 250);
      });
    },
    onInstallLog: (cb) => {
      installLog = cb;
      return () => {
        if (installLog === cb) installLog = null;
      };
    },
    onRemoveServer: (url) => log("onRemoveServer", url),
    onCopy: (text) => log("onCopy", text),
    onCheckServer: async (url) => {
      log("onCheckServer", url);
      return { status: "ok" as const };
    },
    onCloudSetup: () => log("onCloudSetup"),
    onSwitchToLegacy: () => log("onSwitchToLegacy"),
  };
}
