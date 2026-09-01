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
import { ModeSelectStep } from "@/pages/onboarding/ModeSelectStep";
import { ServerSelectStep } from "@/pages/onboarding/ServerSelectStep";
import { SetupTerminalStep } from "@/pages/onboarding/SetupTerminalStep";

/**
 * Outcome of a connect attempt. `needsConfirm` → the URL doesn't look like an
 * Omnigent server (re-try with force); `error` → the connect was rejected and
 * the message should be shown; neither → navigation is underway.
 */
export interface ConnectResult {
  needsConfirm?: boolean;
  error?: string;
}

/** Actions + data the Electron shell supplies to the flow. */
export interface ServerSelectorV2Setup {
  /** Initial server URL to prefill (saved / failed / default). */
  initialUrl: string;
  /** Optional error banner (from the shell's ?error=&url= params). */
  error?: string;
  /** Recently-connected server URLs (most recent first). */
  recentServers: string[];
  /** Organization-provided server URLs. */
  managedServers: string[];
  /** Persist + navigate to a server URL. Resolves `{needsConfirm}` when the URL
   *  doesn't look like an Omnigent server (call again with force), or `{error}`
   *  when the connect was rejected — so the step can show it rather than
   *  silently doing nothing. Navigation on success replaces this page. */
  onConnect: (url: string, force?: boolean) => Promise<ConnectResult>;
  /** Start (or reuse) the local server, then connect to it. Resolves the
   *  outcome so the terminal step can show ready/failed (on success the window
   *  navigates away, so it resolves only on failure in practice). */
  onStartLocal: () => Promise<{ ok: boolean; error?: string }>;
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

type Step = "landing" | "mode" | "server" | "terminal";

// Per-step card dimensions (px). The panel shrinks as steps gain content; the
// card grows for the scrollable server list. Drives the CSS-transition resize.
const CARD: Record<Step, { height: number; panelHeight: number }> = {
  landing: { height: 560, panelHeight: 308 },
  mode: { height: 560, panelHeight: 96 },
  server: { height: 600, panelHeight: 64 },
  terminal: { height: 560, panelHeight: 96 },
};

export function ServerSelectorV2({ setup }: { setup: ServerSelectorV2Setup }) {
  // A failed connect reloads the wizard with an error (from ?error=&url=). That
  // only ever comes from the server-select flow, so open there — otherwise the
  // error banner renders on a step that isn't mounted and stays invisible.
  const [step, setStep] = useState<Step>(setup.error ? "server" : "landing");
  const { height, panelHeight } = CARD[step];

  return (
    <div className="grid min-h-screen place-items-center p-6">
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
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <AnimatedOmnigentPanel height={height} panelHeight={panelHeight}>
        {step === "landing" && (
          <LandingStep
            onGetStarted={() => setStep("mode")}
            onJoinServer={() => setStep("server")}
          />
        )}
        {step === "mode" && (
          <ModeSelectStep
            onBack={() => setStep("landing")}
            onBegin={() => setStep("terminal")}
            onCloudSetup={setup.onCloudSetup}
          />
        )}
        {step === "terminal" && (
          <SetupTerminalStep onStartLocal={setup.onStartLocal} onBack={() => setStep("mode")} />
        )}
        {step === "server" && (
          <ServerSelectStep
            initialUrl={setup.initialUrl}
            error={setup.error}
            recentServers={setup.recentServers}
            managedServers={setup.managedServers}
            onBack={() => setStep("landing")}
            onConnect={setup.onConnect}
            onRemove={setup.onRemoveServer}
            onCopy={setup.onCopy}
            onCheckServer={setup.onCheckServer}
          />
        )}
      </AnimatedOmnigentPanel>

      {step === "landing" && <LandingFooter />}
    </div>
  );
}
