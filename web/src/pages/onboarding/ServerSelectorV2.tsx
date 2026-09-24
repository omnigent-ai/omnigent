/**
 * The Electron server-selector-v2 flow: landing → deployment mode → server
 * select, inside one card that resizes between steps. Mounted only by
 * `server-selector-v2.tsx` (the gated Electron setup page), wired to the native
 * `omnigentSetup` bridge via the `setup` prop.
 */

import { type CSSProperties, useState } from "react";
import { Settings } from "lucide-react";
import { AnimatedOmnigentPanel } from "@/components/onboarding/AnimatedOmnigentPanel";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LandingFooter } from "@/pages/onboarding/LandingFooter";
import { LandingStep } from "@/pages/onboarding/LandingStep";
import { HarnessIconRow, LocalIntroStep } from "@/pages/onboarding/LocalIntroStep";
import { ServerDetailStep } from "@/pages/onboarding/ServerDetailStep";
import { ServerHeroIcons, ServerSelectStep } from "@/pages/onboarding/ServerSelectStep";
import { SetupTerminalStep } from "@/pages/onboarding/SetupTerminalStep";

/**
 * Outcome of a connect attempt. `error` → the connect was rejected and the
 * message should be shown; otherwise navigation is underway.
 */
export interface ConnectResult {
  error?: string;
}

/** Actions + data the Electron shell supplies to the flow. */
export interface ServerSelectorV2Setup {
  /** Initial server URL to prefill (saved / failed / default). */
  initialUrl: string;
  /** Step to open on. "server" jumps straight to the server list ("Connect to
   *  new server…" from a connected window); default is the first-run landing. */
  initialStep?: "server";
  /** Optional error banner (from the shell's ?error=&url= params). */
  error?: string;
  /** Recently-connected server URLs (most recent first). */
  recentServers: string[];
  /** Organization-provided server URLs. */
  managedServers: string[];
  /** Whether the `omnigent` CLI is already installed (returning user). Drives
   *  the "Install" vs "Open" action label and the returning-user start step. */
  installed?: boolean;
  /** Mocks only: route Join / Install actions to the terminal step (which runs
   *  the mocked local-server flow) instead of the no-op connect, so the install
   *  screen is reachable from every path. Never set by the real shell. */
  mockInstall?: boolean;
  /** Persist + navigate to a server URL. Resolves `{error}` when the connect
   *  was rejected — so the step can show it rather than silently doing nothing.
   *  Navigation on success replaces this page. */
  onConnect: (url: string) => Promise<ConnectResult>;
  /** Start (or reuse) the local server, then connect to it. Resolves the
   *  outcome so the terminal step can show ready/failed (on success the window
   *  navigates away, so it resolves only on failure in practice). */
  onStartLocal: () => Promise<{ ok: boolean; error?: string }>;
  /** Subscribe to the local server's startup log lines while it boots; returns
   *  an unsubscribe. Absent on older shells / browser preview → the terminal
   *  step shows the coarse phases only. */
  onSetupLog?: (cb: (line: string) => void) => () => void;
  /** Install the omnigent CLI (macOS). Present only when the CLI is missing and
   *  the shell supports it; absent → the install step is skipped. */
  onInstallCli?: () => Promise<{ ok: boolean; error?: string }>;
  /** Subscribe to the CLI installer's output lines; returns an unsubscribe. */
  onInstallLog?: (cb: (line: string) => void) => () => void;
  /** Remove a recent server from the saved list, if the shell supports it. */
  onRemoveServer?: (url: string) => void;
  /** Copy text to the clipboard via the shell's native bridge. */
  onCopy: (text: string) => void;
  /** Advisory reachability probe for a just-added server URL. */
  onCheckServer: (url: string) => Promise<ServerCheckResult>;
  /** Open the Cloud deploy docs in the user's browser. */
  onCloudSetup: () => void;
  /** Revert to the classic (legacy) setup page. */
  onSwitchToLegacy: () => void;
}

/** Result of the advisory reachability probe. */
export interface ServerCheckResult {
  status: "ok" | "reachable" | "unreachable";
}

type Step = "landing" | "local" | "detail" | "server" | "terminal";

// Per-step card dimensions (px). The panel shrinks as steps gain content; the
// card grows for the scrollable server list. Drives the CSS-transition resize.
const CARD: Record<Step, { height: number; panelHeight: number }> = {
  landing: { height: 560, panelHeight: 308 },
  local: { height: 560, panelHeight: 150 },
  detail: { height: 600, panelHeight: 150 },
  server: { height: 600, panelHeight: 64 },
  terminal: { height: 560, panelHeight: 240 },
};

export function ServerSelectorV2({ setup }: { setup: ServerSelectorV2Setup }) {
  // A failed connect reloads the wizard with an error (from ?error=&url=). That
  // only ever comes from the server-select flow, so open there — otherwise the
  // error banner renders on a step that isn't mounted and stays invisible.
  // "Connect to new server…" also opens the list directly (initialStep). A
  // returning user (CLI installed) skips the welcome and lands on the list too;
  // Back still reaches the landing for the local path.
  const [step, setStep] = useState<Step>(
    setup.error || setup.initialStep === "server" || setup.installed ? "server" : "landing",
  );
  // The preset server picked from the landing split button (drives the detail step).
  const [detailUrl, setDetailUrl] = useState<string | null>(null);
  // What the terminal step should run after any install: start the local server,
  // or connect to a remote URL. Set by whichever action routed to "terminal".
  const [terminalTarget, setTerminalTarget] = useState<
    { kind: "local" } | { kind: "connect"; url: string }
  >({ kind: "local" });
  // Install runs in the terminal step only when the CLI is missing AND in-app
  // install is actually offered (macOS — onInstallCli is present). A returning
  // user ("Open Omnigent"), or any platform without install support, connects
  // directly. Mocks force the install screen to show.
  const needsInstall =
    setup.mockInstall === true || (setup.installed === false && setup.onInstallCli != null);

  // A server pick (list Join / preset detail): install-then-connect when the CLI
  // is missing (route via terminal), else connect straight away. Resolves the
  // ConnectResult so the list can still show a connect error when connecting
  // directly.
  const connect = async (url: string): Promise<ConnectResult> => {
    if (needsInstall) {
      setTerminalTarget({ kind: "connect", url });
      setStep("terminal");
      return {};
    }
    return setup.onConnect(url);
  };
  // Whether the server step is showing its URL-input ("add") view vs the list —
  // reported up so the band can show the hero icons only in the add view.
  const [serverAddMode, setServerAddMode] = useState(false);
  const { height, panelHeight: basePanelHeight } = CARD[step];
  // The server step's add (URL-input) view shows the hero band, which needs the
  // taller panel; the list view keeps the contracted band.
  const panelHeight = step === "server" && serverAddMode ? 150 : basePanelHeight;

  // Panel band: harness icons on the local intro; server hero icons on the
  // detail step and on the server step's add (URL-input) view.
  const bandContent =
    step === "local" ? (
      <HarnessIconRow />
    ) : step === "detail" || (step === "server" && serverAddMode) ? (
      <ServerHeroIcons />
    ) : undefined;

  return (
    <div
      // Center the card in the space above the fixed footer: pb reserves the
      // footer's band so the card never reaches it, and overflow-auto only
      // kicks in when the viewport is too short for the card itself.
      className="grid min-h-screen place-items-center overflow-auto p-6 pb-20"
      style={{ background: "var(--onboarding-wizard-background)" }}
    >
      {/* Top-right cog: settings for this setup surface. no-drag so it's
          clickable over the window's drag strip. */}
      <div
        className="fixed right-3 top-2 z-10"
        style={{ WebkitAppRegion: "no-drag" } as CSSProperties}
      >
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-label="Server selector settings"
            >
              <Settings className="size-4" aria-hidden />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={setup.onSwitchToLegacy}>
              Switch to legacy selector experience
            </DropdownMenuItem>
            {/* Debug aid: flip the theme in place (not persisted). */}
            <DropdownMenuItem onSelect={() => document.documentElement.classList.toggle("dark")}>
              Toggle light/dark mode
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <AnimatedOmnigentPanel
        height={height}
        panelHeight={panelHeight}
        bandContent={bandContent}
        contracted={step === "server" && !serverAddMode}
      >
        {step === "landing" && (
          <LandingStep
            managedServers={setup.managedServers}
            onGetStarted={() => setStep("local")}
            onJoinServer={() => setStep("server")}
            onJoinManaged={(url) => {
              setDetailUrl(url);
              setStep("detail");
            }}
          />
        )}
        {step === "local" && (
          <LocalIntroStep
            installed={setup.installed}
            onBack={() => setStep("landing")}
            onInstall={() => {
              setTerminalTarget({ kind: "local" });
              setStep("terminal");
            }}
          />
        )}
        {step === "detail" && detailUrl !== null && (
          <ServerDetailStep
            url={detailUrl}
            installed={setup.installed}
            onBack={() => setStep("landing")}
            onConnect={connect}
            onCopy={setup.onCopy}
            onShowAll={() => setStep("server")}
          />
        )}
        {step === "terminal" && (
          <SetupTerminalStep
            onInstallCli={needsInstall ? setup.onInstallCli : undefined}
            onInstallLog={setup.onInstallLog}
            onRun={
              terminalTarget.kind === "connect"
                ? async () => {
                    const url = terminalTarget.url;
                    const result = await setup.onConnect(url);
                    // Success navigates the window away; a rejection surfaces as
                    // an error here.
                    return { ok: result.error === undefined, error: result.error };
                  }
                : setup.onStartLocal
            }
            onSetupLog={setup.onSetupLog}
            onBack={() => setStep(terminalTarget.kind === "connect" ? "server" : "local")}
            runningLabel={terminalTarget.kind === "connect" ? "Connecting" : "Starting Omnigent"}
          />
        )}
        {step === "server" && (
          <ServerSelectStep
            initialUrl={setup.initialUrl}
            error={setup.error}
            recentServers={setup.recentServers}
            managedServers={setup.managedServers}
            installed={setup.installed}
            onBack={() => setStep("landing")}
            onConnect={connect}
            onRemove={setup.onRemoveServer}
            onCopy={setup.onCopy}
            onCheckServer={setup.onCheckServer}
            onAddModeChange={setServerAddMode}
          />
        )}
      </AnimatedOmnigentPanel>

      <LandingFooter />
    </div>
  );
}
