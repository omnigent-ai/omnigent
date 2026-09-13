// Onboarding step: start the local server, shown as a small terminal-styled
// status log. The local server daemonizes (omnigent server --background); the
// shell tails its logfile and streams the real startup lines here via
// onSetupLog. We frame those streamed lines with a phase heading + progress bar
// and the real start → ready/failed lifecycle, not fabricated install steps.
// When no stream is available (older shell / browser preview) the log shows a
// single "Starting…" line until ready. On success the window navigates to the
// server and this page is replaced, so "Ready" is only ever briefly visible.

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

type Phase = "starting" | "ready" | "failed";

export function SetupTerminalStep({
  onStartLocal,
  onSetupLog,
  onBack,
}: {
  onStartLocal: () => Promise<{ ok: boolean; error?: string }>;
  onSetupLog?: (cb: (line: string) => void) => () => void;
  onBack: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("starting");
  const [error, setError] = useState<string | undefined>();
  const [lines, setLines] = useState<string[]>([]);
  // Bump to re-run the start effect on retry.
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

  // Subscribe to the streamed startup log lines (if the shell exposes them).
  useEffect(() => {
    if (!onSetupLog) return;
    setLines([]);
    const unsubscribe = onSetupLog((line) => {
      if (alive.current) setLines((prev) => [...prev, line]);
    });
    return unsubscribe;
  }, [onSetupLog, attempt]);

  // Keep the newest line in view as the stream grows.
  useEffect(() => {
    if (logBox.current) logBox.current.scrollTop = logBox.current.scrollHeight;
  }, [lines]);

  useEffect(() => {
    setPhase("starting");
    setError(undefined);
    onStartLocal().then((result) => {
      if (!alive.current) return;
      if (result.ok) setPhase("ready");
      else {
        setPhase("failed");
        setError(result.error);
      }
    });
  }, [onStartLocal, attempt]);

  const streamed = lines.length > 0;
  const phaseLabel =
    phase === "ready"
      ? "Omnigent is ready"
      : phase === "failed"
        ? "Couldn't start Omnigent"
        : "Starting Omnigent";

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-1">
      <div className="mb-2 text-sm font-medium text-foreground">{phaseLabel}</div>
      <div className="mb-3 h-[6px] w-full overflow-hidden rounded-full bg-foreground/[0.06]">
        <div
          className={`h-full rounded-full transition-all duration-300 ease-linear ${
            phase === "failed" ? "bg-destructive/60" : "bg-foreground/25"
          } ${phase === "starting" ? "animate-pulse" : ""}`}
          style={{ width: phase === "starting" ? "60%" : "100%" }}
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
          <div className="text-foreground/25">Starting the local server…</div>
        )}
        {phase === "ready" && (
          <div className="text-[rgb(34,197,94)]">
            <span className="select-none">✓</span> Server ready
          </div>
        )}
        {phase === "failed" && (
          <div className="text-destructive">
            <span className="select-none">✕</span> {error ?? "Could not start the local server."}
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
      : /\b(ready|complete|listening|running on|started)\b/i.test(t)
        ? "text-[rgb(34,197,94)]"
        : "text-foreground/80";
  return <div className={`whitespace-pre-wrap break-words ${color}`}>{text}</div>;
}
