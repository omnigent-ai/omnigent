// Onboarding step: run the install + start/connect sequence, shown as a small
// terminal-styled status log. When the CLI isn't installed yet, an install
// phase runs first (streaming install_oss.sh output via onInstallLog); on
// success the main action runs — start the local server (omnigent server
// --background, streaming its boot log via onSetupLog) or connect to a remote
// server. On success the window navigates away and this page is replaced.
//
// No stream available (older shell / browser preview) → a single phase line.

import { useEffect, useRef, useState } from "react";
import { Copy, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ConnectProgress, SetupResult } from "@/pages/onboarding/ServerSelectorV2";
import { ConnectStatus } from "@/pages/onboarding/primitives";
import { buildFailureDiagnostics } from "@/pages/onboarding/failureDiagnostics";

type Phase = "installing" | "running" | "ready" | "failed";
type CopyState = "idle" | "copied" | "failed";

/** What a failure copy may say about where it happened. Never raw output. */
export interface DiagnosticsContext {
  /** Which runner the run action connects ("laptop", "arca", or a local start). */
  runner?: string;
  /** The server being connected, reduced to its origin before it is copied. */
  serverUrl?: string;
  /** Developer fixtures set this so a copy is labeled as simulated. */
  simulated?: boolean;
}

// Empty-terminal beat before the first install commands stream in, so the
// install step doesn't flash straight from empty into output.
export const WARMUP_MS = 600;

export function SetupTerminalStep({
  onInstallCli,
  onInstallLog,
  onRun,
  onSetupLog,
  onBack,
  onConnectAnyway,
  runningLabel = "Starting Omnigent",
  runningHint = "Starting the local server…",
  connection = null,
  onCancelConnect,
  onCopyDiagnostics,
  diagnosticsContext,
}: {
  /** Install the CLI first (when missing). Absent → skip straight to onRun. */
  onInstallCli?: () => Promise<SetupResult>;
  /** Subscribe to the installer's output lines; returns an unsubscribe. */
  onInstallLog?: (cb: (line: string) => void) => () => void;
  /** The main action after install: start the local server or connect. */
  onRun: () => Promise<SetupResult>;
  /** Subscribe to the run action's log lines (server boot); returns unsubscribe. */
  onSetupLog?: (cb: (line: string) => void) => () => void;
  onBack: () => void;
  /** Open an existing server after setup fails, without installing or starting a runner. */
  onConnectAnyway?: () => void;
  /** Heading + verb for the run phase ("Starting Omnigent" / "Connecting…"). */
  runningLabel?: string;
  /** Placeholder log line for the run phase until its first line streams. */
  runningHint?: string;
  /** Progress of the run phase's server connect (null when not connecting). */
  connection?: ConnectProgress | null;
  onCancelConnect?: () => void;
  /** Copy text to the clipboard; a rejection or `false` shows "Couldn't copy". Absent → no button. */
  onCopyDiagnostics?: (text: string) => void | boolean | Promise<unknown>;
  diagnosticsContext?: DiagnosticsContext;
}) {
  const [phase, setPhase] = useState<Phase>(onInstallCli ? "installing" : "running");
  const [error, setError] = useState<string | undefined>();
  const [errorAction, setErrorAction] = useState<string | undefined>();
  const [errorKind, setErrorKind] = useState<string | undefined>();
  // Set only when the shell sent a curated message; a legacy `error` is display-only.
  const [structuredMessage, setStructuredMessage] = useState<string | undefined>();
  const [failedStage, setFailedStage] = useState<"install" | "connect">("connect");
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const [lines, setLines] = useState<string[]>([]);
  // Bump to re-run the sequence on retry.
  const [attempt, setAttempt] = useState(0);
  // Guard against a resolve landing after unmount (window navigated away).
  const alive = useRef(true);
  const logBox = useRef<HTMLDivElement>(null);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  // Stream install lines while installing, server lines once running. Keyed on
  // the stream source (installing vs not) so a later ready/failed transition
  // doesn't needlessly resubscribe.
  const installing = phase === "installing";
  useEffect(() => {
    const subscribe = installing ? onInstallLog : onSetupLog;
    if (!subscribe) return;
    const unsubscribe = subscribe((line) => {
      if (alive.current) setLines((prev) => [...prev, line]);
    });
    return unsubscribe;
  }, [onInstallLog, onSetupLog, installing]);

  // Keep the newest line in view as the stream grows.
  useEffect(() => {
    if (logBox.current) logBox.current.scrollTop = logBox.current.scrollHeight;
  }, [lines]);

  // Read the latest callbacks without keying the sequence effect on their
  // identity: the parent re-creates onInstallCli/onRun every render, and a
  // deferred server-list update landing mid-install would otherwise restart the
  // effect and launch a SECOND install. The sequence runs once per attempt.
  const onInstallCliRef = useRef(onInstallCli);
  const onRunRef = useRef(onRun);
  const onInstallLogRef = useRef(onInstallLog);
  onInstallCliRef.current = onInstallCli;
  onRunRef.current = onRun;
  onInstallLogRef.current = onInstallLog;

  // Install (if needed) → run, once per attempt (retry bumps `attempt`).
  useEffect(() => {
    let canceled = false;
    setError(undefined);
    setErrorAction(undefined);
    setErrorKind(undefined);
    setStructuredMessage(undefined);
    setCopyState("idle");
    setLines([]);
    (async () => {
      const install = onInstallCliRef.current;
      if (install) {
        setPhase("installing");
        // Hold the empty loader + terminal for a beat before the first install
        // commands stream in — only when there's a stream to show it against.
        if (onInstallLogRef.current) {
          await new Promise((resolve) => {
            setTimeout(resolve, WARMUP_MS);
          });
          if (canceled || !alive.current) return;
        }
        const res = await install();
        if (canceled || !alive.current) return;
        if (!res.ok) {
          setPhase("failed");
          setError(res.errorMessage ?? res.error ?? "Couldn't install the Omnigent CLI.");
          setErrorAction(res.errorAction);
          setErrorKind(res.errorKind);
          setStructuredMessage(res.errorMessage);
          setFailedStage("install");
          return;
        }
        setLines([]);
      }
      setPhase("running");
      const result = await onRunRef.current();
      if (canceled || !alive.current) return;
      if (result.ok) setPhase("ready");
      else {
        setPhase("failed");
        setError(result.errorMessage ?? result.error);
        setErrorAction(result.errorAction);
        setErrorKind(result.errorKind);
        setStructuredMessage(result.errorMessage);
        setFailedStage("connect");
      }
    })();
    return () => {
      canceled = true;
    };
  }, [attempt]);

  // Cycle "." → ".." → "..." on the in-progress title so a slow install/start
  // still reads as alive.
  const inProgress = phase === "installing" || phase === "running";
  const [dots, setDots] = useState(1);
  useEffect(() => {
    if (!inProgress) return;
    setDots(1);
    const timer = setInterval(() => setDots((n) => (n % 3) + 1), 400);
    return () => clearInterval(timer);
  }, [inProgress]);

  const copyDiagnostics = async () => {
    if (!onCopyDiagnostics) return;
    // Only structured, curated fields are used; the legacy combined `error` is
    // the display fallback and stays out of the copy.
    const text = buildFailureDiagnostics({
      surface: "Setup terminal",
      stage: failedStage,
      runner: diagnosticsContext?.runner,
      serverUrl: diagnosticsContext?.serverUrl,
      simulated: diagnosticsContext?.simulated,
      errorKind,
      errorMessage: structuredMessage,
      errorAction,
    });
    try {
      const result = await onCopyDiagnostics(text);
      if (alive.current) setCopyState(result === false ? "failed" : "copied");
    } catch {
      if (alive.current) setCopyState("failed");
    }
  };

  const streamed = lines.length > 0;
  const baseLabel =
    phase === "ready"
      ? "Omnigent is ready"
      : phase === "failed"
        ? "Couldn't set up Omnigent"
        : phase === "installing"
          ? "Installing the Omnigent CLI"
          : runningLabel;
  const phaseLabel = inProgress ? `${baseLabel}${".".repeat(dots)}` : baseLabel;
  const pendingHint = phase === "installing" ? "Installing the CLI…" : runningHint;
  // Coarse progress over real milestones: warmup → install output → server
  // starting → done; without an install, the run's first output and the server
  // open fill those steps. Holds within a step (streaming log + pulse show liveness).
  const progress =
    phase === "ready" || phase === "failed"
      ? 100
      : phase === "running" && (onInstallCli || connection !== null)
        ? 70
        : streamed
          ? 35
          : 10;

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-4">
      <div className="mb-2 text-sm font-medium text-foreground">{phaseLabel}</div>
      <div className="mb-3 h-[6px] w-full overflow-hidden rounded-full bg-foreground/[0.06]">
        <div
          className={`h-full rounded-full transition-all duration-300 ease-linear ${
            phase === "failed" ? "bg-destructive/60" : "bg-foreground/25"
          } ${inProgress ? "animate-pulse" : ""}`}
          style={{ width: `${progress}%` }}
        />
      </div>

      <div
        ref={logBox}
        className="min-h-0 flex-1 overflow-y-auto text-[13px] leading-4"
        style={{ fontFamily: '"SF Mono", Monaco, Consolas, monospace' }}
      >
        {streamed ? (
          lines.map((line, i) => (
            // Streamed log lines have no stable id; index is fine (append-only).
            // eslint-disable-next-line react/no-array-index-key
            <LogLine key={i} text={line} />
          ))
        ) : (
          <div className="text-foreground/25">{pendingHint}</div>
        )}
        {phase === "ready" && (
          <div className="text-[rgb(34,197,94)]">
            <span className="select-none" aria-hidden="true">
              ✓
            </span>{" "}
            Server ready
          </div>
        )}
        {phase === "failed" && (
          <>
            <div role="alert" className="space-y-1">
              <div className="text-destructive">
                <span className="select-none" aria-hidden="true">
                  ✕
                </span>{" "}
                {error ?? "Could not set up Omnigent."}
              </div>
              {errorAction && (
                <div className="text-foreground/80">
                  <span className="select-none" aria-hidden="true">
                    →
                  </span>{" "}
                  {errorAction}
                </div>
              )}
            </div>
            {onCopyDiagnostics && (
              <div className="flex items-center gap-2 pt-2 font-sans">
                <Button variant="outline" size="sm" onClick={copyDiagnostics}>
                  <Copy className="size-3.5" aria-hidden />
                  Copy diagnostics
                </Button>
                <span role="status" aria-live="polite" className="text-xs text-muted-foreground">
                  {copyState === "copied" && "Copied"}
                  {copyState === "failed" && "Couldn't copy. Try again."}
                </span>
              </div>
            )}
          </>
        )}
      </div>

      <ConnectStatus connection={connection} onCancel={onCancelConnect} />

      {phase === "failed" && (
        <div className="mt-3 flex justify-between gap-2">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <div className="flex gap-2">
            {onConnectAnyway && (
              <Button variant="outline" onClick={onConnectAnyway}>
                Continue anyway
              </Button>
            )}
            <Button onClick={() => setAttempt((n) => n + 1)}>
              <RotateCw className="size-4" aria-hidden />
              Retry
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// One raw streamed log line, styled as a terminal row. Raw uvicorn/app logs have
// no command/pending/success structure, so we color by a light severity
// heuristic: errors red, warnings amber, "ready/complete" green, else normal.
function LogLine({ text }: { text: string }) {
  const t = text.trim();
  const color = /\b(error|traceback|exception|fatal|failed)\b/i.test(t)
    ? "text-destructive"
    : /\b(warn|warning)\b/i.test(t)
      ? "text-[rgb(180,120,0)]"
      : /\b(ready|complete|listening|running on|started|installed)\b/i.test(t)
        ? "text-[rgb(34,197,94)]"
        : "text-foreground/80";
  return <div className={`whitespace-pre-wrap break-words ${color}`}>{text}</div>;
}
