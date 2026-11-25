// The agent's pane while its terminal is missing beside a split's live shells: it resumes the
// session once on its own, as the Terminal view does, then offers Resume session if that fails.
// The shells beside it stay mounted throughout.

import { Loader2Icon } from "lucide-react";
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";

/** How long a resumed session has to list its agent's terminal before the pane says so. */
export const AGENT_RECONNECT_TIMEOUT_MS = 10_000;

/** Whether this opening of the view already tried to recover, or saw the agent live. */
export interface AgentRecoveryAllowance {
  attempted: boolean;
}

type Phase = { kind: "idle"; error: string | null } | { kind: "resuming" } | { kind: "connecting" };

export function MissingAgentPane({
  conversationId,
  onResume,
  visible,
  readOnly,
  listSettled,
  allowance,
}: {
  conversationId: string;
  onResume: (() => void | Promise<void>) | undefined;
  visible: boolean;
  readOnly: boolean;
  /** The terminal list has loaded and isn't refetching, so the agent is missing, not late. */
  listSettled: boolean;
  allowance: RefObject<AgentRecoveryAllowance>;
}) {
  const hostOnline = useSessionHostOnline(conversationId);
  const [phase, setPhase] = useState<Phase>({ kind: "idle", error: null });
  const pending = useRef(false);
  const resume = useCallback(
    async (automatic: boolean) => {
      if (!onResume || pending.current) return;
      pending.current = true;
      allowance.current.attempted = true;
      setPhase({ kind: "resuming" });
      try {
        await onResume();
        setPhase({ kind: "connecting" });
      } catch (error) {
        setPhase({
          kind: "idle",
          error: automatic
            ? "Couldn't reconnect the terminal automatically."
            : error instanceof Error && error.message
              ? `Couldn't resume session: ${error.message}`
              : "Couldn't resume session.",
        });
      } finally {
        pending.current = false;
      }
    },
    [onResume, allowance],
  );
  const automatic =
    phase.kind === "idle" &&
    phase.error === null &&
    visible &&
    !readOnly &&
    hostOnline === true &&
    listSettled &&
    onResume !== undefined &&
    !allowance.current.attempted;
  useEffect(() => {
    if (automatic) void resume(true);
  }, [automatic, resume]);
  // The pane gives way once the agent's terminal is listed; until then, say if it never is.
  useEffect(() => {
    if (phase.kind !== "connecting") return;
    const timeout = window.setTimeout(
      () =>
        setPhase({
          kind: "idle",
          error: "The terminal didn't reconnect. Try resuming the session.",
        }),
      AGENT_RECONNECT_TIMEOUT_MS,
    );
    return () => window.clearTimeout(timeout);
  }, [phase.kind]);

  const center = "flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center";
  if (automatic || phase.kind !== "idle") {
    return (
      <div role="status" aria-live="polite" className={center}>
        <Loader2Icon className="size-6 animate-spin text-muted-foreground" aria-hidden />
        <p className="font-medium text-foreground text-ui">Reconnecting…</p>
      </div>
    );
  }
  if (!onResume || readOnly) {
    return <div className={`${center} text-muted-foreground text-ui`}>Terminal unavailable.</div>;
  }
  return (
    <div className={center}>
      <div className="space-y-1">
        <p className="font-medium text-foreground text-ui">The harness is not running.</p>
        <p className="text-muted-foreground text-sm">
          Resume the session to reconnect the terminal.
        </p>
      </div>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        onClick={() => void resume(false)}
        componentId="terminal.split.missing_agent.resume"
      >
        Resume session
      </Button>
      {phase.error && (
        <p role="alert" className="text-destructive text-sm">
          {phase.error}
        </p>
      )}
    </div>
  );
}
