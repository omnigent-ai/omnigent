import {
  AlertCircleIcon,
  BanIcon,
  BellIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  InfoIcon,
  TerminalIcon,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ParsedSystemMessage, SystemMessageKind } from "@/lib/systemMessage";

const KIND_ICON: Record<Exclude<SystemMessageKind, "subagent_wake">, LucideIcon> = {
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
 */
export function SystemMessageView({ message }: SystemMessageViewProps) {
  const [open, setOpen] = useState(false);
  if (message.kind === "subagent_wake") return null;
  const Icon = KIND_ICON[message.kind];
  const hasBody = message.body.trim().length > 0;

  return (
    <div
      className="my-1 flex flex-col items-center gap-1 text-muted-foreground text-xs"
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
        <div className="mt-0.5 max-h-64 max-w-full overflow-auto whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-left text-xs text-muted-foreground">
          {message.body}
        </div>
      )}
    </div>
  );
}
