import { formatTokenCount } from "@/lib/formatCost";
import {
  turnBreakdownTotal,
  type ContextBreakdownAttachment,
  type SessionContextBreakdown,
  type TurnContextBreakdown,
} from "@/lib/sessionsApi";
import { formatBytes } from "@/shell/fileStatusUtils";
import { cn } from "@/lib/utils";

/** Whether a row's figure comes from the provider's report or a size-based estimate. */
type RowSource = "measured" | "estimated" | "unknown";

interface BreakdownRowProps {
  label: string;
  tokens: number | null;
  source: RowSource;
  detail?: string;
  /** Replaces the token figure, e.g. "In workspace". */
  valueLabel?: string;
  nested?: boolean;
  testId?: string;
}

function BreakdownRow({
  label,
  tokens,
  source,
  detail,
  valueLabel,
  nested = false,
  testId,
}: BreakdownRowProps) {
  const value =
    valueLabel ??
    (tokens == null
      ? "Not measured"
      : `${source === "estimated" ? "~" : ""}${formatTokenCount(tokens)}`);
  return (
    <li
      data-testid={testId}
      data-source={source}
      className={cn("flex min-w-0 flex-col gap-0.5", nested && "pl-3")}
    >
      <div className="flex min-w-0 items-baseline gap-2">
        <span
          className={cn(
            "min-w-0 flex-1 truncate",
            nested ? "text-xs text-muted-foreground" : "text-sm text-foreground",
          )}
          title={label}
        >
          {label}
        </span>
        <span
          className={cn(
            "shrink-0 tabular-nums",
            nested ? "text-xs text-muted-foreground" : "text-sm text-foreground",
          )}
        >
          {value}
        </span>
        <span className="w-14 shrink-0 text-right text-[11px] text-muted-foreground">
          {source === "measured" ? "measured" : source === "estimated" ? "estimate" : ""}
        </span>
      </div>
      {detail ? <span className="text-xs text-muted-foreground">{detail}</span> : null}
    </li>
  );
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function attachmentRow(attachment: ContextBreakdownAttachment, index: number) {
  const name = attachment.filename ?? "Attachment";
  const size = attachment.bytes != null ? formatBytes(attachment.bytes) : null;
  const label = size ? `${name} · ${size}` : name;
  const key = `${attachment.fileId ?? name}-${index}`;
  if (attachment.delivery === "workspace") {
    return (
      <BreakdownRow
        key={key}
        nested
        label={label}
        tokens={null}
        source="unknown"
        valueLabel="In workspace"
      />
    );
  }
  if (attachment.delivery === "missing") {
    return (
      <BreakdownRow
        key={key}
        nested
        label={label}
        tokens={null}
        source="unknown"
        valueLabel="Not delivered"
      />
    );
  }
  return (
    <BreakdownRow
      key={key}
      nested
      label={label}
      tokens={attachment.estimatedTokens}
      source={attachment.estimatedTokens == null ? "unknown" : "estimated"}
      valueLabel={attachment.estimatedTokens == null ? "Size only" : undefined}
    />
  );
}

/** An attachment under the runner's total: its own estimate would not add up to it. */
function reportedAttachmentRow(attachment: ContextBreakdownAttachment, index: number) {
  if (attachment.delivery !== "inline") return attachmentRow(attachment, index);
  const name = attachment.filename ?? "Attachment";
  const size = attachment.bytes != null ? formatBytes(attachment.bytes) : null;
  return (
    <BreakdownRow
      key={`${attachment.fileId ?? name}-${index}`}
      nested
      label={size ? `${name} · ${size}` : name}
      tokens={null}
      source="unknown"
      valueLabel="Sent inline"
    />
  );
}

/** Detail for the Other row; says so when the estimates outgrow the measured total. */
function otherDetail(breakdown: SessionContextBreakdown, tokensUsed: number): string {
  if (itemizedEstimate(breakdown) > tokensUsed) {
    return "The estimates above exceed the measured total, so they run high";
  }
  return breakdown.reported
    ? "Content the harness adds itself and the last reply"
    : "Tool schemas, content the harness adds itself, and the last reply";
}

function toolsDetail(breakdown: SessionContextBreakdown, toolCount?: number): string {
  const parts: string[] = [];
  if (toolCount != null) parts.push(plural(toolCount, "tool"));
  if (breakdown.mcpServers.length > 0) parts.push(`MCP: ${breakdown.mcpServers.join(", ")}`);
  if (toolCount == null && breakdown.builtinToolCount > 0)
    parts.push(plural(breakdown.builtinToolCount, "built-in tool"));
  if (toolCount == null && breakdown.localToolCount > 0)
    parts.push(plural(breakdown.localToolCount, "agent tool"));
  return parts.length > 0 ? parts.join(" · ") : "Harness and Omnigent tools only";
}

function historyDetail(breakdown: SessionContextBreakdown, itemCount?: number): string {
  // The runner counts what it sent, so the server's scan cap does not apply.
  const prefix = itemCount == null && breakdown.historyPartial ? "At least " : "";
  const count = itemCount ?? breakdown.historyItemCount;
  const noun = count === 1 ? "message or tool call" : "messages and tool calls";
  const parts = [`${prefix}${count} ${noun}`];
  if (breakdown.summarizedItemCount > 0) {
    const atLeast = breakdown.summarizedPartial ? "at least " : "";
    const items = breakdown.summarizedItemCount === 1 ? "item" : "items";
    parts.push(`${atLeast}${breakdown.summarizedItemCount} earlier ${items} replaced by a summary`);
  }
  return parts.join(" · ");
}

/**
 * Sum of the token estimates the breakdown itemizes, used to derive the
 * unattributed remainder of the measured total.
 */
export function itemizedEstimate(breakdown: SessionContextBreakdown): number {
  if (breakdown.reported) return turnBreakdownTotal(breakdown.reported);
  const attachments = breakdown.attachments.reduce(
    (sum, attachment) =>
      attachment.delivery === "inline" ? sum + (attachment.estimatedTokens ?? 0) : sum,
    0,
  );
  return (
    (breakdown.systemPromptTokens ?? 0) +
    breakdown.skillListingTokens +
    attachments +
    breakdown.historyTokens
  );
}

/** Rows from the runner's own request, preferred over the server's estimates. */
function ReportedRows({
  breakdown,
  reported,
}: {
  breakdown: SessionContextBreakdown;
  reported: TurnContextBreakdown;
}) {
  return (
    <>
      <BreakdownRow
        testId="context-row-system-prompt"
        label="System prompt"
        tokens={reported.systemTokens}
        source="estimated"
        detail="Instructions the runner sent on the last turn"
      />
      <BreakdownRow
        testId="context-row-tools"
        label="Tool definitions"
        tokens={reported.toolTokens}
        source="estimated"
        detail={toolsDetail(breakdown, reported.toolCount)}
      />
      {reported.attachmentCount > 0 || breakdown.attachments.length > 0 ? (
        <>
          <BreakdownRow
            testId="context-row-attachments"
            label={`Attachments (${reported.attachmentCount})`}
            tokens={reported.attachmentTokens}
            source="estimated"
            detail={
              reported.unestimatedAttachmentCount > 0
                ? `Plus ${reported.unestimatedAttachmentCount} sent without an estimate, counted in Other`
                : undefined
            }
          />
          {breakdown.attachments.map(reportedAttachmentRow)}
        </>
      ) : null}
      <BreakdownRow
        testId="context-row-history"
        label="Conversation history"
        tokens={reported.historyTokens}
        source="estimated"
        detail={historyDetail(breakdown, reported.historyItemCount)}
      />
      <BreakdownRow
        testId="context-row-message"
        label="Current message"
        tokens={reported.messageTokens}
        source="estimated"
      />
    </>
  );
}

/** What a request rejected as too long for the window attempted to send. */
function FailedAttempt({ attempt }: { attempt: TurnContextBreakdown }) {
  const rows: [string, number, string?][] = [
    ["System prompt", attempt.systemTokens],
    ["Tool definitions", attempt.toolTokens, plural(attempt.toolCount, "tool")],
    [
      "Conversation history",
      attempt.historyTokens,
      attempt.historyItemCount === 1
        ? "1 message or tool call"
        : `${attempt.historyItemCount} messages and tool calls`,
    ],
    [`Attachments (${attempt.attachmentCount})`, attempt.attachmentTokens],
    ["Current message", attempt.messageTokens],
  ];
  return (
    <div
      className="flex flex-col gap-1.5 border-t border-border pt-2"
      data-testid="context-failed-attempt"
    >
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium text-foreground">Last request was too long</span>
        <span className="text-xs tabular-nums text-muted-foreground">
          About {formatTokenCount(turnBreakdownTotal(attempt))} tokens attempted, by source
        </span>
      </div>
      <ul className="flex flex-col gap-1.5" aria-label="What the rejected request carried">
        {rows
          .filter(([, tokens]) => tokens > 0)
          .map(([label, tokens, detail]) => (
            <BreakdownRow
              key={label}
              label={label}
              tokens={tokens}
              source="estimated"
              detail={detail}
            />
          ))}
      </ul>
    </div>
  );
}

/**
 * Popover body splitting the context window into what fills it.
 *
 * The header and free space come from the provider-reported total of the last
 * turn. The other rows are size-based estimates: from the request the runner
 * sent when it reports one, else from the server's stored content. "Other" is
 * the measured total minus those estimates.
 */
export function ComposerContextBreakdown({
  contextWindow,
  tokensUsed,
  breakdown,
  status,
}: {
  contextWindow: number;
  tokensUsed: number;
  breakdown: SessionContextBreakdown | undefined;
  status: "pending" | "error" | "success";
}) {
  const usedPct = Math.round(Math.min(tokensUsed / contextWindow, 1) * 100);
  const free = Math.max(contextWindow - tokensUsed, 0);
  return (
    <div className="flex flex-col gap-2" data-testid="composer-context-breakdown">
      <div className="flex flex-col gap-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-sm font-medium text-foreground">Context window</span>
          <span className="text-sm tabular-nums text-foreground">{usedPct}% used</span>
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">
          {formatTokenCount(tokensUsed)} / {formatTokenCount(contextWindow)} tokens, measured on the
          last turn
        </span>
      </div>
      {status === "pending" ? (
        <p className="text-xs text-muted-foreground">Estimating what fills the window…</p>
      ) : status === "error" || breakdown == null ? (
        <p className="text-xs text-muted-foreground">
          A breakdown isn't available for this session.
        </p>
      ) : (
        <ul className="flex flex-col gap-1.5" aria-label="What fills the context window">
          {breakdown.reported ? (
            <ReportedRows breakdown={breakdown} reported={breakdown.reported} />
          ) : (
            <ServerEstimateRows breakdown={breakdown} />
          )}
          <BreakdownRow
            testId="context-row-other"
            label="Other"
            tokens={Math.max(tokensUsed - itemizedEstimate(breakdown), 0)}
            source="estimated"
            detail={otherDetail(breakdown, tokensUsed)}
          />
          <BreakdownRow
            testId="context-row-free"
            label="Free space"
            tokens={free}
            source="measured"
          />
        </ul>
      )}
      {status === "success" && breakdown?.failedAttempt ? (
        <FailedAttempt attempt={breakdown.failedAttempt} />
      ) : null}
      <p className="text-[11px] text-muted-foreground">
        Estimates come from content size, about {breakdown?.bytesPerToken ?? 4} bytes per token.
      </p>
    </div>
  );
}

/** Rows estimated by the server from stored content, for harnesses without a report. */
function ServerEstimateRows({ breakdown }: { breakdown: SessionContextBreakdown }) {
  const inlineAttachments = breakdown.attachments.filter((a) => a.delivery === "inline");
  const unestimated = inlineAttachments.filter((a) => a.estimatedTokens == null).length;
  const attachmentTokens = inlineAttachments.reduce(
    (sum, attachment) => sum + (attachment.estimatedTokens ?? 0),
    0,
  );
  return (
    <>
      <BreakdownRow
        testId="context-row-system-prompt"
        label="System prompt"
        tokens={breakdown.systemPromptTokens}
        source={breakdown.systemPromptTokens == null ? "unknown" : "estimated"}
        detail="Agent instructions and Omnigent guidance"
      />
      {breakdown.skillCount > 0 ? (
        <BreakdownRow
          testId="context-row-skills"
          label={`Skill listing (${breakdown.skillCount})`}
          tokens={breakdown.skillListingTokens}
          source="estimated"
        />
      ) : null}
      <BreakdownRow
        testId="context-row-tools"
        label="Tool definitions"
        tokens={null}
        source="unknown"
        detail={toolsDetail(breakdown)}
      />
      {breakdown.attachments.length > 0 ? (
        <>
          <BreakdownRow
            testId="context-row-attachments"
            label={`Attachments (${breakdown.attachments.length})`}
            tokens={attachmentTokens}
            source="estimated"
            detail={
              unestimated > 0
                ? `Plus ${unestimated} sent without an estimate, counted in Other`
                : undefined
            }
          />
          {breakdown.attachments.map(attachmentRow)}
        </>
      ) : null}
      <BreakdownRow
        testId="context-row-history"
        label="Conversation history"
        tokens={breakdown.historyTokens}
        source="estimated"
        detail={historyDetail(breakdown)}
      />
    </>
  );
}
