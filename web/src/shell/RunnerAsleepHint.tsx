import { CloudOffIcon, PauseCircleIcon } from "lucide-react";

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
 * misreads that, so name the sandbox; a message starts it again.
 */
export function RunnerAsleepHint({ sandboxOffline = false }: { sandboxOffline?: boolean }) {
  const Icon = sandboxOffline ? CloudOffIcon : PauseCircleIcon;
  return (
    <div className="flex flex-col items-start gap-1 px-2 py-1.5 text-muted-foreground text-sm">
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        <Icon className="size-3.5 shrink-0" />
        {sandboxOffline ? "Sandbox is offline" : "Agent is asleep"}
      </span>
      <span>
        {sandboxOffline
          ? "Send a message in the chat to start it again."
          : "Send a message in the chat to reconnect."}
      </span>
    </div>
  );
}
