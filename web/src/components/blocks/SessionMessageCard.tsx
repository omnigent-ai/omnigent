// Rows for agent-to-agent messages: "From @…" on received ones, "To @…" on sent ones.
// An accepted send gets an outlined row in place of its tool call; others keep a tool row.

import { useState, type ReactNode } from "react";
import {
  ChevronRightIcon,
  ChevronUpIcon,
  CornerUpRightIcon,
  Loader2Icon,
  TriangleAlertIcon,
  XCircleIcon,
  type LucideIcon,
} from "lucide-react";
import { useScopedConversationId } from "@/components/chat/conversationScope";
import { useSessionLabel } from "@/hooks/useSessionLabel";
import type {
  OutboundSessionMessage,
  OutboundSessionMessageStatus,
} from "@/lib/sessionMessageOrigin";
import { firstLine } from "@/lib/toolValue";
import { cn } from "@/lib/utils";
import { FilePathAwareMessageResponse, SessionMentionPill } from "./ChatMarkdown";
import { formatToolDuration, useElapsedDuration } from "./ToolCard";

interface SessionMessageLabelProps {
  /** The other session, e.g. ``"9b8b26c8edfd40d781c0d13a04fa5c0b"``. */
  sessionId: string;
  /** Session being viewed; its subagents name the pill before any lookup. */
  currentSessionId: string | null;
  /** Words before the pill, e.g. ``"From"`` or ``"Could not confirm send to"``. */
  verb: string;
  icon: LucideIcon;
  /** Extra icon classes, e.g. ``"animate-spin text-info"``. */
  iconClassName?: string;
  /** Extra pill classes, e.g. ``"min-w-0"`` so a long title truncates. */
  pillClassName?: string;
  className?: string;
  testId: string;
}

/**
 * "From @sender" or "To @recipient": an icon, a verb and the other session's pill,
 * or "another session" while its title is unknown or unreadable.
 */
export function SessionMessageLabel({
  sessionId,
  currentSessionId,
  verb,
  icon: Icon,
  iconClassName,
  pillClassName,
  className,
  testId,
}: SessionMessageLabelProps) {
  return (
    <div
      data-testid={testId}
      className={cn("flex shrink-0 items-center gap-1 text-xs text-muted-foreground", className)}
    >
      <Icon className={cn("size-3 shrink-0", iconClassName)} />
      <span className="shrink-0 whitespace-nowrap">{verb}</span>
      <SessionReference
        sessionId={sessionId}
        currentSessionId={currentSessionId}
        pillClassName={pillClassName}
      />
    </div>
  );
}

/**
 * The other session's pill, or "another session" while its title is unknown or unreadable.
 *
 * :param sessionId: The session to name, e.g. ``"9b8b26c8edfd40d781c0d13a04fa5c0b"``.
 * :param currentSessionId: Session being viewed; its subagents name the pill first.
 * :param pillClassName: Extra pill classes, e.g. ``"min-w-0"`` so a long title truncates.
 */
function SessionReference({
  sessionId,
  currentSessionId,
  pillClassName,
}: {
  sessionId: string;
  currentSessionId: string | null;
  pillClassName?: string;
}) {
  const label = useSessionLabel(sessionId, currentSessionId);
  if (!label) return <span>another session</span>;
  return (
    <SessionMentionPill id={sessionId} isSubagent={label.isSubagent} className={pillClassName}>
      @{label.title}
    </SessionMentionPill>
  );
}

type UnsentStatus = Exclude<OutboundSessionMessageStatus, "sent">;

interface UnsentRow {
  verb: string;
  icon: LucideIcon;
  iconClassName: string;
  /** Text after the pill; a refused send shows its error instead. */
  note: string | null;
}

const UNSENT_ROWS: Record<UnsentStatus, UnsentRow> = {
  sending: {
    verb: "Sending to",
    icon: Loader2Icon,
    iconClassName: "animate-spin text-info",
    note: null,
  },
  not_sent: {
    verb: "Not sent to",
    icon: XCircleIcon,
    iconClassName: "text-destructive",
    note: null,
  },
  // The send may already be queued, so the row must not invite a second one.
  unconfirmed: {
    verb: "Could not confirm send to",
    icon: TriangleAlertIcon,
    iconClassName: "text-warning",
    note: "don't resend automatically",
  },
};

// Outline only: a fill marks what this chat received (user bubbles, From…).
const SENT_SURFACE = "flex w-fit max-w-[min(100%,640px)] px-3 ring-1 ring-border";
const TOGGLE_CLASS = cn(
  "rounded-sm text-muted-foreground outline-none transition-colors hover:text-foreground",
  "focus-visible:ring-3 focus-visible:ring-ring/50",
);

interface MessageRowProps {
  message: OutboundSessionMessage;
  currentSessionId: string | null;
  expanded: boolean;
  onToggle: () => void;
}

interface OutboundSessionMessageCardProps {
  /** The classified send, from ``outboundSessionMessage``. */
  message: OutboundSessionMessage;
  /** Page-clock seconds the call started, e.g. ``12.5``; times a send still in flight. */
  startedAt?: number | null;
  /** Completed runtime in seconds, when known. */
  duration?: number;
}

/**
 * The row a message this session sent gets in place of its tool call: "To @recipient"
 * and its first line once the server accepted it, expandable to the whole text.
 */
export function OutboundSessionMessageCard({
  message,
  startedAt,
  duration,
}: OutboundSessionMessageCardProps) {
  const currentSessionId = useScopedConversationId();
  const [expanded, setExpanded] = useState(false);
  const elapsed = useElapsedDuration(message.status === "sending" ? startedAt : null);
  const rowProps = {
    message,
    currentSessionId,
    expanded,
    onToggle: () => setExpanded((open) => !open),
  };
  if (message.status === "sent") return <SentMessage {...rowProps} />;
  return <UnsentMessage {...rowProps} status={message.status} duration={duration ?? elapsed} />;
}

/** An accepted send: a one-line ``rounded-lg`` row, or ``rounded-2xl`` with the full text. */
function SentMessage({ message, currentSessionId, expanded, onToggle }: MessageRowProps) {
  const label = (
    <SessionMessageLabel
      sessionId={message.recipientId}
      currentSessionId={currentSessionId}
      verb="To"
      icon={CornerUpRightIcon}
      className={cn("min-w-0", expanded ? "shrink" : "max-w-64")}
      pillClassName="min-w-0"
      testId="message-destination"
    />
  );
  return expanded ? (
    <ExpandedSentMessage label={label} body={message.body} onCollapse={onToggle} />
  ) : (
    <CollapsedSentMessage label={label} body={message.body} onExpand={onToggle} />
  );
}

/** The default view: the label and the message's first line, like a collapsed From…. */
function CollapsedSentMessage({
  label,
  body,
  onExpand,
}: {
  label: ReactNode;
  body: string;
  onExpand: () => void;
}) {
  return (
    <div
      data-testid="outbound-session-message"
      data-status="sent"
      className={cn(SENT_SURFACE, "items-center gap-2 rounded-lg py-1.5")}
    >
      {label}
      <button
        type="button"
        onClick={onExpand}
        aria-expanded={false}
        aria-label="Show message"
        data-testid="expand-sent-message"
        className={cn("flex min-w-0 items-center gap-1 text-xs", TOGGLE_CLASS)}
      >
        <span className="min-w-0 truncate">{firstLine(body)}</span>
        <ChevronRightIcon className="size-3 shrink-0" />
      </button>
    </div>
  );
}

/** The expanded view: the label with a collapse control, then the whole message. */
function ExpandedSentMessage({
  label,
  body,
  onCollapse,
}: {
  label: ReactNode;
  body: string;
  onCollapse: () => void;
}) {
  return (
    <div
      data-testid="outbound-session-message"
      data-status="sent"
      className={cn(SENT_SURFACE, "flex-col gap-2 rounded-2xl py-2")}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        {label}
        <button
          type="button"
          onClick={onCollapse}
          aria-expanded
          aria-label="Collapse message"
          data-testid="collapse-sent-message"
          className={cn("shrink-0", TOGGLE_CLASS)}
        >
          <ChevronUpIcon className="size-3" />
        </button>
      </div>
      <SentText text={body} />
    </div>
  );
}

/**
 * A send the server has not accepted, as a tool row: status icon, medium verb, the
 * recipient, a note, the runtime and a chevron, expanding to its error and text.
 */
function UnsentMessage({
  message,
  status,
  currentSessionId,
  expanded,
  onToggle,
  duration,
}: MessageRowProps & { status: UnsentStatus; duration: number | undefined }) {
  const row = UNSENT_ROWS[status];
  const Icon = row.icon;
  const note = status === "not_sent" ? message.error : row.note;
  return (
    <div data-testid="outbound-session-message" data-status={status} className="w-full">
      <div className="flex w-full items-center gap-1.5 py-0.5 text-muted-foreground text-ui">
        <Icon className={cn("size-3.5 shrink-0", row.iconClassName)} />
        <span data-testid="message-destination" className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0 whitespace-nowrap font-medium text-foreground">{row.verb}</span>
          <SessionReference
            sessionId={message.recipientId}
            currentSessionId={currentSessionId}
            pillClassName="min-w-0"
          />
        </span>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={expanded ? "Hide message" : "Show message"}
          data-testid="toggle-unsent-message"
          className={cn("flex min-w-0 flex-1 items-center gap-1.5 text-left", TOGGLE_CLASS)}
        >
          <span className="min-w-0 flex-1 truncate">{note === null ? null : `· ${note}`}</span>
          {duration !== undefined && (
            <span className="shrink-0 tabular-nums opacity-70">{formatToolDuration(duration)}</span>
          )}
          <ChevronRightIcon
            className={cn("size-3.5 shrink-0 transition-transform", expanded && "rotate-90")}
          />
        </button>
      </div>
      {expanded && <UnsentDetail error={message.error} body={message.body} />}
    </div>
  );
}

/** What an unsent message would have said, under the error its send reported. */
function UnsentDetail({ error, body }: { error: string | null; body: string }) {
  return (
    <div className="mt-1 ml-2 flex flex-col gap-2 border-l py-1 pl-3 text-sm">
      {error !== null && <p className="text-muted-foreground">{error}</p>}
      <SentText text={body} />
    </div>
  );
}

/** The message text; raw HTML shows as written, so markup can't hide part of it. */
function SentText({ text }: { text: string }) {
  return (
    <FilePathAwareMessageResponse className="text-message" breaks literalHtml mode="static">
      {text}
    </FilePathAwareMessageResponse>
  );
}
