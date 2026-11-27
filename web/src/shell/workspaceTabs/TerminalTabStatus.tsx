// What a shell tab shows before its terminal can: connecting, or why it can't and what to do.
// The sandbox's states use the same notice and Resume every other workspace pane does.

import { AlertCircleIcon, WifiOffIcon } from "lucide-react";
import { type ReactNode, useEffect } from "react";

import {
  SandboxResumeNoticeView,
  type SandboxResumeNoticeState,
} from "@/components/SandboxResumeNotice";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { CliCommandBlock } from "@/shell/CliCommandBlock";
import { LOCAL_RECONNECT_DESCRIPTION, LOCAL_RECONNECT_TITLE } from "@/shell/ReconnectSessionDialog";

import type { TerminalTabConnection } from "./terminalTabConnection";

export const VIEWER_RESUME_HINT = "Ask someone who can edit this session to resume it.";
export const OWNER_RECONNECT_HINT = "Ask the session's owner to reconnect it.";

export function TerminalTabStatus({
  connection,
  sandboxNotice,
  onResumeSession,
  resumingSession = false,
  onTryAgain,
  localReconnectCommand,
}: {
  connection: TerminalTabConnection;
  sandboxNotice: SandboxResumeNoticeState | null;
  /** Starts the session's runner; absent for viewers who can't. */
  onResumeSession?: () => void;
  /** A session Resume is on its way. */
  resumingSession?: boolean;
  onTryAgain: () => void;
  /**
   * The command that relaunches a session that isn't host-bound, as Reconnect shows it; absent
   * for anyone but the owner, since only the owner can attach a runner.
   */
  localReconnectCommand?: string;
}) {
  const stalled = stalledReason(connection);
  useEffect(() => {
    // Console text a bug report carries: why a shell tab couldn't connect.
    if (stalled !== null) console.info(`[terminal] connect stalled: reason=${stalled}`);
  }, [stalled]);
  switch (connection.kind) {
    case "connecting":
      return (
        <div
          data-testid="terminal-tab-connecting"
          className="flex flex-1 items-center justify-center gap-2 text-ui text-muted-foreground"
        >
          <Spinner />
          Connecting…
        </div>
      );
    case "sandbox":
      return sandboxNotice ? (
        <SandboxResumeNoticeView view={sandboxNotice} purpose="use this terminal" />
      ) : null;
    case "host_offline":
      return (
        <TerminalTabMessage
          icon={<WifiOffIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />}
          title="Host is offline"
          detail="Use Reconnect host in the host menu below the chat to bring this terminal back."
        />
      );
    case "local_stranded":
      return (
        <TerminalTabMessage
          icon={<WifiOffIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />}
          title={LOCAL_RECONNECT_TITLE}
          detail={localReconnectCommand ? LOCAL_RECONNECT_DESCRIPTION : OWNER_RECONNECT_HINT}
          action={
            localReconnectCommand ? (
              <div className="w-full max-w-sm text-left">
                <CliCommandBlock
                  command={localReconnectCommand}
                  testIdPrefix="terminal-tab-reconnect"
                />
              </div>
            ) : null
          }
        />
      );
    case "runner_asleep":
      return (
        <TerminalTabMessage
          icon={<WifiOffIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />}
          title="Session isn't running"
          detail={
            onResumeSession
              ? (connection.failure ?? "Resume it to use this terminal.")
              : // A viewer can't resume it, so the message names who can.
                [connection.failure, VIEWER_RESUME_HINT].filter(Boolean).join(" ")
          }
          failed={connection.failure !== null}
          action={
            onResumeSession ? (
              <Button
                type="button"
                size="sm"
                onClick={onResumeSession}
                loading={resumingSession}
                disabled={resumingSession}
                componentId="terminal.tab.resume_session"
              >
                {connection.failure ? "Try Again" : "Resume session"}
              </Button>
            ) : null
          }
        />
      );
    case "unreachable":
      return (
        <TerminalTabMessage
          icon={<AlertCircleIcon className="size-4 shrink-0 text-destructive" aria-hidden />}
          title="Couldn't connect to this terminal"
          detail="Check your network, then try again."
          failed
          action={
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={onTryAgain}
              componentId="terminal.tab.try_again"
            >
              Try Again
            </Button>
          }
        />
      );
    case "ended":
      return (
        <TerminalTabMessage
          icon={<AlertCircleIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />}
          title="This shell has ended"
          detail="Open a new terminal from the + menu to keep working."
        />
      );
    case "live":
      return null;
  }
}

/** Why the tab can't show its terminal, for diagnostics, or null while it's on its way. */
function stalledReason(connection: TerminalTabConnection): string | null {
  switch (connection.kind) {
    case "sandbox":
      return connection.status === "resuming" ? null : `sandbox_${connection.status}`;
    case "host_offline":
    case "local_stranded":
    case "runner_asleep":
    case "unreachable":
      return connection.kind;
    default:
      return null;
  }
}

function TerminalTabMessage({
  icon,
  title,
  detail,
  failed = false,
  action,
}: {
  icon: ReactNode;
  title: string;
  detail: string;
  failed?: boolean;
  action?: ReactNode;
}) {
  return (
    <div
      role={failed ? "alert" : "status"}
      data-testid="terminal-tab-status"
      className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-8 text-center"
    >
      <div className="max-w-sm space-y-1">
        <p className="flex items-center justify-center gap-2 font-medium text-foreground text-ui">
          {icon}
          {title}
        </p>
        <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{detail}</p>
      </div>
      {action}
    </div>
  );
}
