import { CloudOffIcon, PauseCircleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { SandboxWakePhase } from "@/hooks/useSandboxAutoWake";

/** A panel's own wake of the asleep sandbox, for users who may restart it. */
export interface SandboxWakeHintState {
  phase: SandboxWakePhase;
  error: string | null;
  onWake: () => void;
}

/**
 * Shown in a workspace panel when the session's runner is bound but not
 * connected (HTTP 503 → {@link RunnerOfflineError}). For host-bound
 * sessions this recovers as soon as the user sends a message (the host
 * relaunches the runner), so we guide them there instead of surfacing a
 * raw "Failed to load: 503". Shared by the Changed and All file tabs so
 * both read identically.
 *
 * `sandboxOffline`: the session's managed sandbox itself is down (stopped
 * while idle, for example), not just its runner. "The agent is asleep"
 * misreads that, so name the sandbox; a message starts it again. With
 * `wake`, the panel starts the sandbox itself and shows that progress.
 */
export function RunnerAsleepHint({
  sandboxOffline = false,
  wake,
}: {
  sandboxOffline?: boolean;
  wake?: SandboxWakeHintState;
}) {
  if (sandboxOffline && wake?.phase === "waking") {
    return (
      <div
        role="status"
        className="flex flex-col items-start gap-1 px-2 py-1.5 text-muted-foreground text-sm"
      >
        <span className="flex items-center gap-1.5 font-medium text-foreground">
          <Spinner aria-hidden className="size-3.5 shrink-0" />
          Starting sandbox…
        </span>
        <span>Files will load when it's back online.</span>
      </div>
    );
  }
  const Icon = sandboxOffline ? CloudOffIcon : PauseCircleIcon;
  return (
    <div className="flex flex-col items-start gap-1 px-2 py-1.5 text-muted-foreground text-sm">
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        <Icon className="size-3.5 shrink-0" />
        {sandboxOffline ? "Sandbox is offline" : "Agent is asleep"}
      </span>
      {sandboxOffline && wake ? (
        <>
          {wake.error && <span role="alert">{wake.error}</span>}
          <Button type="button" variant="outline" size="sm" className="mt-1" onClick={wake.onWake}>
            {wake.phase === "failed" ? "Try again" : "Start sandbox"}
          </Button>
        </>
      ) : (
        <span>
          {sandboxOffline
            ? "Send a message in the chat to start it again."
            : "Send a message in the chat to reconnect."}
        </span>
      )}
    </div>
  );
}
