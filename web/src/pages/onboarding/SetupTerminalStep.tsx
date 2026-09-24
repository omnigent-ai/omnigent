// Onboarding step: run the install + start/connect sequence, shown as a small
// terminal-styled status log. When the CLI isn't installed yet, an install
// phase runs first (streaming install_oss.sh output via onInstallLog); on
// success the main action runs — start the local server (omnigent server
// --background, streaming its boot log via onSetupLog) or connect to a remote
// server. On success the window navigates away and this page is replaced.
//
// No stream available (older shell / browser preview) → a single phase line.

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

type Phase = "installing" | "running" | "ready" | "failed";

export function SetupTerminalStep({
  onInstallCli,
  onInstallLog,
  onRun,
  onSetupLog,
  onBack,
  runningLabel = "Starting Omnigent",
}: {
  /** Install the CLI first (when missing). Absent → skip straight to onRun. */
  onInstallCli?: () => Promise<{ ok: boolean; error?: string }>;
  /** Subscribe to the installer's output lines; returns an unsubscribe. */
  onInstallLog?: (cb: (line: string) => void) => () => void;
  /** The main action after install: start the local server or connect. */
  onRun: () => Promise<{ ok: boolean; error?: string }>;
  /** Subscribe to the run action's log lines (server boot); returns unsubscribe. */
  onSetupLog?: (cb: (line: string) => void) => () => void;
  onBack: () => void;
  /** Heading + verb for the run phase ("Starting Omnigent" / "Connecting…"). */
  runningLabel?: string;
}) {
  const [phase, setPhase] = useState<Phase>(onInstallCli ? "installing" : "running");
  const [error, setError] = useState<string | undefined>();
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
  onInstallCliRef.current = onInstallCli;
  onRunRef.current = onRun;

  // Install (if needed) → run, once per attempt (retry bumps `attempt`).
  useEffect(() => {
    let canceled = false;
    setError(undefined);
    setLines([]);
    (async () => {
      const install = onInstallCliRef.current;
      if (install) {
        setPhase("installing");
        const res = await install();
        if (canceled || !alive.current) return;
        if (!res.ok) {
          setPhase("failed");
          setError(res.error ?? "Couldn't install the Omnigent CLI.");
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
        setError(result.error);
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
  const pendingHint = phase === "installing" ? "Installing the CLI…" : "Starting the local server…";

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-4">
      <div className="mb-2 text-sm font-medium text-foreground">{phaseLabel}</div>
      <div className="mb-3 h-[6px] w-full overflow-hidden rounded-full bg-foreground/[0.06]">
        <div
          className={`h-full rounded-full transition-all duration-300 ease-linear ${
            phase === "failed" ? "bg-destructive/60" : "bg-foreground/25"
          } ${phase === "installing" || phase === "running" ? "animate-pulse" : ""}`}
          style={{ width: phase === "ready" || phase === "failed" ? "100%" : "60%" }}
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
            <span className="select-none">✓</span> Server ready
          </div>
        )}
        {phase === "failed" && (
          <div className="text-destructive">
            <span className="select-none">✕</span> {error ?? "Could not set up Omnigent."}
          </div>
        )}
      </div>

      {phase === "failed" && (
        <div className="mt-3 flex gap-2">
          <Button variant="outline" className="flex-1" onClick={onBack}>
            Back
          </Button>
          <Button className="flex-1" onClick={() => setAttempt((n) => n + 1)}>
            Retry
          </Button>
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
