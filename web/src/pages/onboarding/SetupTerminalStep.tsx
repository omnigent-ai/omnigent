// Onboarding step: start the local server, shown as a small terminal-styled
// status log. Phases reflect what the shell actually observes — the local
// server daemonizes (omnigent server --background), so there is no live install
// stream to tail; we show the real start → ready/failed lifecycle, not
// fabricated install steps. On success the window navigates to the server and
// this page is replaced, so "Ready" is only ever briefly visible.

import { useEffect, useRef, useState } from "react";
import { CircleCheck, CircleX, LoaderCircle } from "lucide-react";
import { Button } from "@/components/ui/button";

type Phase = "starting" | "ready" | "failed";

export function SetupTerminalStep({
  onStartLocal,
  onBack,
}: {
  onStartLocal: () => Promise<{ ok: boolean; error?: string }>;
  onBack: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("starting");
  const [error, setError] = useState<string | undefined>();
  // Bump to re-run the start effect on retry.
  const [attempt, setAttempt] = useState(0);
  // Guard against a resolve landing after unmount (window navigated away).
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

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

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <h1 className="mb-3 pt-1 text-center text-sm text-foreground">Starting Omnigent</h1>

      <div className="flex-1 rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs">
        <Line state={phase === "starting" ? "running" : "done"} text="Starting the local server…" />
        {phase === "ready" && <Line state="done" text="Server ready" />}
        {phase === "failed" && (
          <Line state="error" text={error ?? "Could not start the local server."} />
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

function Line({ state, text }: { state: "running" | "done" | "error"; text: string }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      {state === "running" && (
        <LoaderCircle
          className="mt-0.5 size-3.5 shrink-0 animate-spin text-muted-foreground"
          aria-hidden
        />
      )}
      {state === "done" && (
        <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-success" aria-hidden />
      )}
      {state === "error" && (
        <CircleX className="mt-0.5 size-3.5 shrink-0 text-destructive" aria-hidden />
      )}
      <span className={state === "error" ? "text-destructive" : "text-foreground"}>{text}</span>
    </div>
  );
}
