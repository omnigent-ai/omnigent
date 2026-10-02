import {
  AlertCircleIcon,
  BanIcon,
  BellIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  InfoIcon,
  TerminalIcon,
  UsersIcon,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ParsedSystemMessage, SystemMessageKind } from "@/lib/systemMessage";

const KIND_ICON: Record<
  Exclude<SystemMessageKind, "subagent_wake" | "teammate_message" | "teammate_finished">,
  LucideIcon
> = {
  task_completed: CheckCircle2Icon,
  task_failed: AlertCircleIcon,
  task_cancelled: BanIcon,
  timer_fired: BellIcon,
  terminal_idle: TerminalIcon,
  interrupted: BanIcon,
  generic: InfoIcon,
};

interface SystemMessageViewProps {
  message: ParsedSystemMessage;
}

/**
 * Centered, muted marker for runtime-injected `[System: ...]` user-role
 * messages (task completion, timer firings, terminal-idle events). The
 * body — tool output, error+traceback, or timer note — is collapsed by
 * default and reveals on click.
 *
 * Sub-agent auto-wake notices are model-facing control traffic. They remain
 * in history so the parent agent drains its inbox, but the web UI hides them
 * because the Agents rail already owns that status.
 *
 * Claude agent-teams deliveries render as a teammate card instead: the
 * teammate's name, its one-line summary, and the prose (or final result)
 * stay readable without a click.
 */
export function SystemMessageView({ message }: SystemMessageViewProps) {
  const [open, setOpen] = useState(false);
  if (message.kind === "subagent_wake") return null;
  if (message.kind === "teammate_message" || message.kind === "teammate_finished") {
    return <TeammateMessageView message={message} />;
  }
  const Icon = KIND_ICON[message.kind];
  const hasBody = message.body.trim().length > 0;

  return (
    <div
      className="my-1 flex flex-col items-center gap-1 text-muted-foreground text-sm"
      data-testid="system-message"
      data-system-kind={message.kind}
    >
      {hasBody ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-1.5 rounded px-1.5 py-0.5 hover:text-foreground"
          aria-expanded={open}
        >
          <Icon className="size-3.5 shrink-0" />
          <span>
            <strong className="font-semibold">System:</strong> {message.label}
          </span>
          <ChevronRightIcon
            className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
          />
        </button>
      ) : (
        <div className="flex items-center gap-1.5 px-1.5 py-0.5">
          <Icon className="size-3.5 shrink-0" />
          <span>
            <strong className="font-semibold">System:</strong> {message.label}
          </span>
        </div>
      )}
      {hasBody && open && (
        <div className="mt-0.5 max-h-64 max-w-full overflow-auto whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-left text-sm text-muted-foreground">
          {message.body}
        </div>
      )}
    </div>
  );
}

function TeammateMessageView({ message }: SystemMessageViewProps) {
  const teammate = message.teammate;
  const finished = message.kind === "teammate_finished";
  const body = message.body.trim();
  let heading = "";
  if (finished) heading = " finished";
  else if (teammate?.summary) heading = ` · ${teammate.summary}`;
  return (
    <div
      className="my-1 flex max-w-[640px] flex-col gap-1 rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-sm"
      data-testid="teammate-message"
      data-system-kind={message.kind}
      data-teammate-id={teammate?.id}
    >
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <UsersIcon className="size-3.5 shrink-0" />
        <span className="truncate">
          <strong className="font-semibold text-foreground">@{teammate?.id ?? "teammate"}</strong>
          {heading}
        </span>
      </div>
      {body && <div className="whitespace-pre-wrap text-foreground">{body}</div>}
    </div>
  );
}
