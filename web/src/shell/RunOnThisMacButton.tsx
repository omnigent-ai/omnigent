// One-click "Run on this Mac" — spawn a host on the user's own machine.
//
// A host is a machine that runs agent sessions and dials OUT to the connected
// server; normally a user starts one with `omnigent host` in a terminal. Inside
// the Electron desktop shell we can fork the process for them, so this button
// turns that into one click. It renders ONLY in the desktop shell when the main
// process reports a usable runtime — in a plain browser (or an older shell) it
// returns null, and the surfaces that embed it keep their CLI-instructions
// fallback.
//
// The button drives a small state machine: idle → starting → polling → online,
// with an error branch. "polling" waits for the freshly-spawned daemon to show
// up via the main process's own status (`getLocalHostStatus().running`), which
// reads the same daemon registry the CLI does. Kept free of react-query / the
// server host list so it can be dropped into any surface (dialogs, the sidebar)
// without a QueryClientProvider in scope.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  CircleHelpIcon,
  Loader2Icon,
  MonitorIcon,
  TriangleAlertIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  getLocalHostStatus,
  isElectronShell,
  startLocalHost,
  type LocalHostStatus,
} from "@/lib/nativeBridge";

/** While polling, how often to re-check the main process, and the overall cap. */
const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 45_000;

type Phase = "idle" | "starting" | "polling" | "online" | "error";

export function RunOnThisMacButton({
  serverUrl,
  className,
  onHostOnline,
}: {
  /** The connected server URL (from `getCliServerUrl()`). */
  serverUrl: string;
  className?: string;
  /** Called once the host comes online — lets a surface close its dialog. */
  onHostOnline?: () => void;
}) {
  // The main process's local-host capability/status for this server.
  // `undefined` = still loading; `null` = no capability (hide entirely).
  const [capability, setCapability] = useState<LocalHostStatus | null | undefined>(undefined);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  const onHostOnlineRef = useRef(onHostOnline);
  onHostOnlineRef.current = onHostOnline;

  // Resolve capability once on mount (only meaningful inside Electron).
  useEffect(() => {
    if (!isElectronShell()) {
      setCapability(null);
      return;
    }
    let cancelled = false;
    void getLocalHostStatus(serverUrl).then((status) => {
      if (cancelled) return;
      setCapability(status);
      if (status?.running) setPhase("online");
    });
    return () => {
      cancelled = true;
    };
  }, [serverUrl]);

  const markOnline = useCallback(() => {
    setPhase("online");
    onHostOnlineRef.current?.();
  }, []);

  // While polling, poll the main process's own status (which reads the daemon
  // registry), with a timeout.
  useEffect(() => {
    if (phase !== "polling") return;
    let cancelled = false;
    const startedAt = Date.now();
    const interval = window.setInterval(() => {
      void getLocalHostStatus(serverUrl).then((status) => {
        if (cancelled) return;
        if (status?.running) {
          markOnline();
        } else if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
          setError("Taking longer than expected — check the host menu in a moment.");
          setPhase("error");
        }
      });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [phase, serverUrl, markOnline]);

  const handleStart = useCallback(async () => {
    setError(null);
    setPhase("starting");
    const result = await startLocalHost(serverUrl);
    if (result.ok) {
      setPhase("polling");
    } else {
      setError(result.error ?? "Couldn't start a host on this Mac.");
      setPhase("error");
    }
  }, [serverUrl]);

  // Not in the desktop shell, or the shell reports no capability → render
  // nothing so the embedding surface shows its CLI fallback instead.
  if (capability === undefined || capability === null) return null;

  // Runtime missing: the CLI isn't installed on this machine. Offer a clear,
  // non-actionable hint rather than a button that can only fail.
  if (!capability.runtimeAvailable) {
    return (
      <div
        className={cn("flex items-center gap-2 text-xs text-muted-foreground", className)}
        data-testid="run-on-this-mac-no-runtime"
      >
        <MonitorIcon className="size-4 shrink-0 opacity-60" />
        <span>Install the Omnigent CLI to run a host on this Mac.</span>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex" aria-label="Why this is unavailable">
                <CircleHelpIcon className="size-3.5" />
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-64">
              The desktop app couldn't find the <code>omnigent</code> CLI. Install it (e.g.{" "}
              <code>uv tool install omnigent</code>), then reopen this menu.
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
    );
  }

  if (phase === "online") {
    return (
      <div
        className={cn("flex items-center gap-2 text-xs text-green-600", className)}
        data-testid="run-on-this-mac-online"
      >
        <CheckIcon className="size-4 shrink-0" />
        <span>Running on this Mac</span>
      </div>
    );
  }

  const busy = phase === "starting" || phase === "polling";

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <Button
        type="button"
        variant="secondary"
        size="sm"
        disabled={busy}
        onClick={handleStart}
        data-testid="run-on-this-mac"
        className="justify-center"
      >
        {busy ? (
          <Loader2Icon className="size-3.5 animate-spin" />
        ) : (
          <MonitorIcon className="size-3.5" />
        )}
        {phase === "starting"
          ? "Starting…"
          : phase === "polling"
            ? "Connecting…"
            : "Run on this Mac"}
      </Button>
      {phase === "error" && error && (
        <p
          className="flex items-center gap-1.5 text-xs text-destructive"
          data-testid="run-on-this-mac-error"
        >
          <TriangleAlertIcon className="size-3.5 shrink-0" />
          <span>{error}</span>
        </p>
      )}
      {phase === "idle" && capability.needsLogin && (
        <p
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          data-testid="run-on-this-mac-needs-login"
        >
          <TriangleAlertIcon className="size-3.5 shrink-0" />
          <span>You may need to sign in first for this server.</span>
        </p>
      )}
    </div>
  );
}
