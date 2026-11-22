import { Fragment } from "react";

import { formatTokenCount } from "@/lib/formatCost";
import type {
  ContextBreakdownAttachment,
  ContextBreakdownCategory,
  ContextCategorySource,
  SessionContextBreakdown,
} from "@/lib/sessionsApi";
import { formatBytes } from "@/shell/fileStatusUtils";
import { cn } from "@/lib/utils";

interface BreakdownRowProps {
  label: string;
  tokens: number | null;
  source: ContextCategorySource;
  detail?: string | null;
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

/** The row title for a category the server resolved. */
function categoryLabel(category: ContextBreakdownCategory): string {
  switch (category.kind) {
    case "system_prompt":
      return "System prompt";
    case "skills":
      return category.count == null ? "Skill listing" : `Skill listing (${category.count})`;
    case "tools":
      return "Tool definitions";
    case "attachments":
      return category.count == null ? "Attachments" : `Attachments (${category.count})`;
    case "history":
      return "Conversation history";
    case "message":
      return "Current message";
    case "other":
      return "Other";
    case "free":
      return "Free space";
    default:
      // A kind added by a newer server still gets a readable row.
      return String(category.kind).replaceAll("_", " ");
  }
}

/**
 * Popover body splitting the context window into what fills it.
 *
 * The server resolves every row, including the remainder and the free space,
 * against the measured total it read with them; this only renders them. The
 * ring's own figures fill the header until that response arrives.
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
  const measured = status === "success" ? breakdown?.measured : null;
  const total = measured?.totalTokens ?? tokensUsed;
  const windowTokens = measured?.contextWindow || contextWindow;
  const usedPct = Math.round(Math.min(total / windowTokens, 1) * 100);
  return (
    <div className="flex flex-col gap-2" data-testid="composer-context-breakdown">
      <div className="flex flex-col gap-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-sm font-medium text-foreground">Context window</span>
          <span className="text-sm tabular-nums text-foreground">{usedPct}% used</span>
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">
          {formatTokenCount(total)} / {formatTokenCount(windowTokens)} tokens, measured on the last
          turn
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
          {breakdown.categories.map((category) => (
            <Fragment key={`${category.origin}-${category.kind}`}>
              <BreakdownRow
                testId={`context-row-${category.kind.replaceAll("_", "-")}`}
                label={categoryLabel(category)}
                tokens={category.tokens}
                source={category.source}
                detail={category.detail}
              />
              {category.kind === "attachments" ? breakdown.attachments.map(attachmentRow) : null}
            </Fragment>
          ))}
        </ul>
      )}
      <p className="text-[11px] text-muted-foreground">
        Estimates come from content size, about {breakdown?.bytesPerToken ?? 4} bytes per token.
      </p>
    </div>
  );
}
