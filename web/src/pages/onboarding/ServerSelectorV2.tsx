/**
 * The Electron server-selector-v2 flow: landing → deployment mode → server
 * select, inside one card that resizes between steps. Mounted only by
 * `server-selector-v2.tsx` (the gated Electron setup page), wired to the native
 * `omnigentSetup` bridge via the `setup` prop.
 */

import { type CSSProperties, useRef, useState } from "react";
import { Settings } from "lucide-react";
import { AnimatedOmnigentPanel } from "@/components/onboarding/AnimatedOmnigentPanel";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LandingFooter } from "@/pages/onboarding/LandingFooter";
import { LandingStep } from "@/pages/onboarding/LandingStep";
import { HarnessIconRow, LocalIntroStep } from "@/pages/onboarding/LocalIntroStep";
import { type Runner, RunnerStep } from "@/pages/onboarding/RunnerStep";
import {
  isLocalInstall,
  ServerHeroIcons,
  ServerSelectStep,
} from "@/pages/onboarding/ServerSelectStep";
import { SetupTerminalStep } from "@/pages/onboarding/SetupTerminalStep";

/**
 * Outcome of a connect attempt. `error` → the connect was rejected and the
 * message should be shown; `cancelled` → the user backed out (Cancel, or closed
 * the workspace picker); otherwise navigation is underway.
 */
export interface ConnectResult {
  error?: string;
  cancelled?: boolean;
}

/** What an in-flight connect is waiting on: browser sign-in, or the server page. */
export type ConnectPhase = "connecting" | "authenticating";

/** An in-flight connect as the steps show it: its phase (or a pending Cancel),
 *  and why a Cancel didn't take. */
export interface ConnectProgress {
  phase: ConnectPhase | "cancelling";
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
  /** Display names for managed servers, server URL → name (from the MDM URL). */
  managedServerNames?: Record<string, string>;
  /** Whether the `omnigent` CLI is already installed. Drives the "Install" vs
   *  "Start"/"Open" action label and whether install runs first. */
  installed?: boolean;
  /** Has connected to any server before (returning user). The shell counts MDM
   *  presets too, which `recentServers` excludes, so it outlives the list. */
  connectedBefore?: boolean;
  /** start-local would reuse a healthy server (checked at load) → the local
   *  intro and its terminal read "Open"/"Opening" rather than "Start". */
  localServerRunning?: boolean;
  /** Mocks only: route Join / Install actions to the terminal step (which runs
   *  the mocked local-server flow) instead of the no-op connect, so the install
   *  screen is reachable from every path. Never set by the real shell. */
  mockInstall?: boolean;
  /** Persist + navigate to a server URL. Resolves `{error}` when the connect
   *  was rejected — so the step can show it rather than silently doing nothing.
   *  Navigation on success replaces this page. `onPhase` reports progress. */
  onConnect: (url: string, onPhase?: (phase: ConnectPhase) => void) => Promise<ConnectResult>;
  /** Cancel the in-flight onConnect. Resolves whether the shell cancelled it
   *  (onConnect then resolves `{cancelled}`); false → it's already finishing. */
  onCancelConnect?: () => Promise<boolean>;
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
  /** Runners the "Where do you work today?" step offers for `url`, and whether
   *  its host CLI comes bundled (no CLI install). Absent → this laptop only. */
  getRunnerOptions?: (url: string) => Promise<{ remote: boolean; bundledCli?: boolean }>;
  /** Connect the picked runner to `url` before opening it. Absent → the runner
   *  step just opens the server. */
  onConnectRunner?: (url: string, runner: Runner) => Promise<{ ok: boolean; error?: string }>;
  /** Subscribe to onConnectRunner's output lines; returns an unsubscribe. */
  onRunnerLog?: (cb: (line: string) => void) => () => void;
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
  /** Disable "Switch to legacy" — the env var pins the selector on, so it can't
   *  take effect. */
  switchToLegacyDisabled?: boolean;
  /** Set the wizard's live color scheme (System/Light/Dark), if the shell
   *  supports it. Absent → the theme submenu is hidden. */
  onSetColorScheme?: (scheme: "light" | "dark" | "system") => void;
  /** The shell's current color-scheme source, to seed the radio (themeSource
   *  survives navigation, so it may be non-system on return to setup). */
  initialColorScheme?: "system" | "light" | "dark";
}

/** Result of the advisory reachability probe. */
export interface ServerCheckResult {
  status: "ok" | "reachable" | "unreachable";
}

type Step = "landing" | "local" | "runner" | "server" | "terminal";

// Per-step card dimensions (px). The panel shrinks as steps gain content; the
// card grows for the scrollable server list. Drives the CSS-transition resize.
const CARD: Record<Step, { height: number; panelHeight: number }> = {
  landing: { height: 560, panelHeight: 308 },
  local: { height: 560, panelHeight: 150 },
  runner: { height: 560, panelHeight: 150 },
  server: { height: 600, panelHeight: 64 },
  terminal: { height: 560, panelHeight: 240 },
};

export function ServerSelectorV2({ setup }: { setup: ServerSelectorV2Setup }) {
  // With MDM presets everyone starts on the landing: its dropdown holds recents
  // and a URL field, and it shows connect errors itself.
  const mdm = setup.managedServers.length > 0;
  // A failed connect reloads with an error (?error=&url=) whose banner lives on
  // the server step, so open there; "Connect to new server…" too (initialStep).
  const failedOrForced = setup.error !== undefined || setup.initialStep === "server";
  // Returning users land on the server list when it has something to pick; new
  // users — or one who cleared every server — land on the welcome.
  const returning = setup.connectedBefore === true || setup.recentServers.length > 0;
  const hasServers = setup.recentServers.length > 0 || mdm;
  const [step, setStep] = useState<Step>(
    !mdm && (failedOrForced || (returning && hasServers)) ? "server" : "landing",
  );
  const [landingError, setLandingError] = useState(setup.error);
  // Wizard color scheme radio. Seeded from the shell's current source (which
  // survives navigation), defaulting to "system" when the shell doesn't report.
  const [colorScheme, setColorScheme] = useState<"system" | "light" | "dark">(
    setup.initialColorScheme ?? "system",
  );
  // The server picked on the MDM landing, whether its runner step offers the
  // remote environment, and whether its host CLI is bundled.
  const [runnerTarget, setRunnerTarget] = useState<{
    url: string;
    remote: boolean;
    bundledCli: boolean;
  } | null>(null);
  const [runnerError, setRunnerError] = useState<string>();
  // Bumped per pick, so a slow lookup can't replace a newer pick's runner step.
  const runnerPick = useRef(0);
  const pickRunnerFor = async (url: string) => {
    const pick = ++runnerPick.current;
    // A failed lookup falls back to this laptop only.
    const options = await setup.getRunnerOptions?.(url).catch(() => undefined);
    if (pick !== runnerPick.current) return;
    setRunnerTarget({
      url,
      remote: options?.remote === true,
      bundledCli: options?.bundledCli === true,
    });
    setRunnerError(undefined);
    setStep("runner");
  };
  // What the terminal step runs after any install (Back → `back`): start the
  // local server (a picked local install's `url` opens as-is when up), or
  // connect to a URL, first connecting the picked runner when set.
  const [terminalTarget, setTerminalTarget] = useState<
    | { kind: "local"; back: Step; url?: string }
    | { kind: "connect"; back: Step; url: string; runner?: Runner; skipInstall?: boolean }
  >({ kind: "local", back: "local" });
  // Install runs in the terminal step only when the CLI is missing AND in-app
  // install is actually offered (macOS — onInstallCli is present). An installed
  // CLI, or any platform without install support, connects directly. Mocks
  // force the install screen to show.
  const needsInstall =
    setup.mockInstall === true || (setup.installed === false && setup.onInstallCli != null);

  // The shell's progress on an in-flight onConnect (null when idle), shown by
  // whichever step started it so a long browser sign-in never looks frozen.
  const [connection, setConnection] = useState<ConnectProgress | null>(null);
  // One connect at a time: a second would supersede the first in the shell.
  const connecting = useRef(false);
  // The shell's latest phase, restored when a Cancel doesn't take.
  const shellPhase = useRef<ConnectPhase>("connecting");
  const connectToServer = async (url: string): Promise<ConnectResult> => {
    if (connecting.current) return { cancelled: true };
    connecting.current = true;
    shellPhase.current = "connecting";
    setConnection({ phase: "connecting" });
    try {
      return await setup.onConnect(url, (phase) => {
        shellPhase.current = phase;
        setConnection((c) => (c && c.phase !== "cancelling" ? { ...c, phase } : c));
      });
    } finally {
      connecting.current = false;
      setConnection(null);
    }
  };
  // Cancel shows "Cancelling…" until the shell confirms; a refusal (the connect
  // is already finishing) or failure restores the phase with the reason.
  const cancelConnect = setup.onCancelConnect
    ? async () => {
        setConnection((c) => c && { phase: "cancelling" });
        let error = "The connection is still finishing. Please try again.";
        try {
          if (await setup.onCancelConnect?.()) return;
        } catch (e) {
          error = e instanceof Error ? e.message : "Could not cancel the connection.";
        }
        setConnection((c) => c && { phase: shellPhase.current, error });
      }
    : undefined;

  // A server pick (list Join / runner step): install-then-connect when the CLI
  // is missing (route via terminal), else connect straight away. Resolves the
  // ConnectResult so the list can still show a connect error when connecting
  // directly.
  const connect = async (url: string, back: Step = "server"): Promise<ConnectResult> => {
    // The local install is checked in the terminal step, so a stopped one starts.
    if (isLocalInstall(url)) {
      setTerminalTarget({ kind: "local", back, url });
      setStep("terminal");
      return {};
    }
    if (needsInstall) {
      setTerminalTarget({ kind: "connect", back, url });
      setStep("terminal");
      return {};
    }
    return connectToServer(url);
  };
  // MDM landing: a new user picks a runner first; a returning user just opens it.
  const joinFromLanding = async (url: string) => {
    if (!returning) return pickRunnerFor(url);
    setLandingError(undefined);
    const result = await connect(url, "landing");
    if (result.error) setLandingError(result.error);
  };
  // Connect from the terminal: success navigates away, a rejection or cancel
  // shows there (with Retry/Back) instead of a "ready" that never opens.
  const connectInTerminal = async (url: string) => {
    const result = await connectToServer(url);
    if (result.cancelled) return { ok: false, error: "Connection cancelled." };
    return { ok: result.error === undefined, error: result.error };
  };
  // Checked at run time (Retry re-checks): a picked local install that's up opens
  // that exact URL; one that's down starts like "Get started locally".
  const runTerminal = async () => {
    const t = terminalTarget;
    if (t.kind === "connect" && t.runner && setup.onConnectRunner) {
      const res = await setup.onConnectRunner(t.url, t.runner);
      if (!res.ok) return res;
    }
    if (t.kind === "connect") return connectInTerminal(t.url);
    if (t.url !== undefined && (await setup.onCheckServer(t.url)).status !== "unreachable")
      return connectInTerminal(t.url);
    return setup.onStartLocal();
  };
  const terminalRunner = terminalTarget.kind === "connect" ? terminalTarget.runner : undefined;
  const skipInstall = terminalTarget.kind === "connect" && terminalTarget.skipInstall === true;
  const terminalCopy = terminalRunningCopy(
    terminalRunner,
    terminalTarget.kind,
    setup.localServerRunning === true,
  );
  // Whether the server step is showing its URL-input ("add") view vs the list —
  // reported up so the band can show the hero icons only in the add view.
  const [serverAddMode, setServerAddMode] = useState(false);
  const { height, panelHeight: basePanelHeight } = CARD[step];
  // The server step's add (URL-input) view shows the hero band, which needs the
  // taller panel; the list view keeps the contracted band.
  const panelHeight = step === "server" && serverAddMode ? 150 : basePanelHeight;

  // Panel band: harness icons on the local intro; server hero icons on the
  // runner step and on the server step's add (URL-input) view.
  const bandContent =
    step === "local" ? (
      <HarnessIconRow />
    ) : step === "runner" || (step === "server" && serverAddMode) ? (
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
            <DropdownMenuItem
              onSelect={setup.onSwitchToLegacy}
              disabled={setup.switchToLegacyDisabled}
            >
              Switch to legacy selector experience
            </DropdownMenuItem>
            {setup.onSetColorScheme && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel>Appearance</DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={colorScheme}
                  onValueChange={(v) => {
                    const scheme = v as "system" | "light" | "dark";
                    setColorScheme(scheme);
                    setup.onSetColorScheme?.(scheme);
                  }}
                >
                  <DropdownMenuRadioItem value="system">System (default)</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </>
            )}
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
            managedServerNames={setup.managedServerNames}
            recentServers={setup.recentServers}
            error={landingError}
            connection={connection}
            onCancelConnect={cancelConnect}
            onGetStarted={() => setStep("local")}
            onJoinServer={() => setStep("server")}
            onJoinManaged={joinFromLanding}
            onJoinUrl={joinFromLanding}
          />
        )}
        {step === "local" && (
          <LocalIntroStep
            installed={setup.installed}
            startsLocal={!setup.localServerRunning}
            onBack={() => setStep("landing")}
            onInstall={() => {
              setTerminalTarget({ kind: "local", back: "local" });
              setStep("terminal");
            }}
          />
        )}
        {step === "runner" && runnerTarget !== null && (
          <RunnerStep
            remoteAvailable={runnerTarget.remote}
            // A bundled host CLI skips the install, so the action only opens.
            installed={setup.installed || runnerTarget.bundledCli}
            error={runnerError}
            connection={connection}
            onCancelConnect={cancelConnect}
            onBack={() => setStep("landing")}
            onInstall={async (runner) => {
              if (!setup.onConnectRunner) {
                setRunnerError(undefined);
                const result = await connect(runnerTarget.url, "runner");
                if (result.error) setRunnerError(result.error);
                return;
              }
              // No local install for a remote runner, or when the host CLI is bundled.
              setTerminalTarget({
                kind: "connect",
                back: "runner",
                url: runnerTarget.url,
                runner,
                skipInstall: runner === "remote" || runnerTarget.bundledCli,
              });
              setStep("terminal");
            }}
          />
        )}
        {step === "terminal" && (
          <SetupTerminalStep
            onInstallCli={needsInstall && !skipInstall ? setup.onInstallCli : undefined}
            onInstallLog={setup.onInstallLog}
            onRun={runTerminal}
            onSetupLog={terminalRunner ? setup.onRunnerLog : setup.onSetupLog}
            onBack={() => setStep(terminalTarget.back)}
            runningLabel={terminalCopy.label}
            runningHint={terminalCopy.hint}
            connection={connection}
            onCancelConnect={cancelConnect}
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
            connection={connection}
            onCancelConnect={cancelConnect}
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

/** Terminal heading + empty-log hint for what the run phase is doing. */
function terminalRunningCopy(
  runner: Runner | undefined,
  kind: "local" | "connect",
  localServerRunning: boolean,
): { label: string; hint: string } {
  if (runner === "remote") {
    return {
      label: "Connecting your remote environment",
      hint: "Starting your remote environment…",
    };
  }
  if (runner === "local") {
    return { label: "Connecting this laptop", hint: "Connecting this laptop to the server…" };
  }
  if (kind === "connect") return { label: "Connecting", hint: "Connecting to the server…" };
  return localServerRunning
    ? { label: "Opening Omnigent", hint: "Connecting to the local server…" }
    : { label: "Starting Omnigent", hint: "Starting the local server…" };
}
