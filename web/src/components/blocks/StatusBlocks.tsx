// Inline status indicators for non-tool, non-text, non-reasoning blocks.
// Each is small enough to live in one file.
//
// - ErrorBanner: destructive Alert with `[source]` + code + message.
// - RetryIndicator: muted one-liner about an in-flight retry.
// - CompactionMarker: permanent marker shown after compaction completes.
//   The in-progress state renders as a Shimmer in ChatPage, mirroring
//   the "Working…" indicator.

import { BrainCircuitIcon, ChevronRightIcon, RotateCcwIcon, ShieldXIcon } from "lucide-react";
import { useMemo } from "react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { shortModelName } from "@/components/CostRoutingControl";
import { cn } from "@/lib/utils";
import {
  COMPACT_TRANSCRIPT_CARD_CLASS,
  TOOL_SURFACE_WIDTH_CLASS,
  TRANSCRIPT_CARD_BODY_CLASS,
  TRANSCRIPT_CARD_META_CLASS,
  TRANSCRIPT_CARD_TITLE_CLASS,
} from "./toolSurface";

interface ErrorBannerProps {
  message: string;
  source: string;
  code: string;
}

/**
 * Restrained inline error card for `error` blocks. Falls back to `code`
 * when `message` is empty (matches the reducer's intent — never show a
 * blank panel even when the LLM error payload omits the message).
 */
export function ErrorBanner({ message, source, code }: ErrorBannerProps) {
  const display = message || code || "Unknown error";
  return (
    <Alert className="min-w-0 max-w-full grid-cols-[18px_minmax(0,1fr)] gap-x-2.5 overflow-hidden border-[0.5px] border-destructive/30 bg-destructive/[0.045] px-3 py-2.5 [box-shadow:none] dark:border-destructive/35 dark:bg-destructive/[0.07]">
      <span
        aria-hidden="true"
        className="row-span-2 mt-px grid size-[18px] shrink-0 place-items-center rounded-full bg-destructive/15 font-semibold text-card-meta text-destructive leading-none dark:bg-destructive/20"
      >
        !
      </span>
      <AlertTitle
        className={cn(
          "col-start-2 min-w-0 break-words font-semibold text-destructive [overflow-wrap:anywhere]",
          TRANSCRIPT_CARD_TITLE_CLASS,
        )}
      >
        Error{source ? ` · ${source}` : ""}
        {code && message ? ` · ${code}` : ""}
      </AlertTitle>
      <AlertDescription className="col-start-2 min-w-0 max-w-full overflow-hidden">
        <span
          className={cn(
            "mt-0.5 block max-w-full whitespace-pre-wrap break-words font-mono text-foreground/90 [overflow-wrap:anywhere] [text-wrap:wrap]",
            TRANSCRIPT_CARD_BODY_CLASS,
          )}
        >
          {display}
        </span>
      </AlertDescription>
    </Alert>
  );
}

interface PolicyDeniedBannerProps {
  reason: string;
  phase: string;
}

/**
 * Warning banner for policy denials. Uses the `default` alert variant
 * (amber/warning tone) to distinguish from hard errors (destructive red).
 */
export function PolicyDeniedBanner({ reason, phase }: PolicyDeniedBannerProps) {
  return (
    <Alert className="border-warning/20 bg-warning/[0.04] dark:border-warning/25 dark:bg-warning/[0.065]">
      <ShieldXIcon className="text-warning-foreground" />
      <AlertTitle className={TRANSCRIPT_CARD_TITLE_CLASS}>
        Blocked by policy{phase ? ` · ${phase}` : ""}
      </AlertTitle>
      <AlertDescription className={TRANSCRIPT_CARD_BODY_CLASS}>{reason}</AlertDescription>
    </Alert>
  );
}

interface RetryIndicatorProps {
  source: string;
  attempt: number;
  maxAttempts: number;
  delaySeconds: number;
}

/**
 * Compact line that signals "we hit a transient failure and the server
 * is going to retry." No banner; reads more like a log line.
 */
export function RetryIndicator({
  source,
  attempt,
  maxAttempts,
  delaySeconds,
}: RetryIndicatorProps) {
  return (
    <div className="flex items-center gap-2 text-muted-foreground text-xs">
      <RotateCcwIcon className="size-3" />
      <span>
        Retrying {source} · attempt {attempt}/{maxAttempts}
        {delaySeconds > 0 ? ` · waiting ${delaySeconds.toFixed(1)}s` : ""}
      </span>
    </div>
  );
}

/**
 * Subtle inline marker that the conversation was compacted (older
 * history was summarized to fit context). The in-progress state is
 * rendered as a `Shimmer` in `ChatPage` to match the "Working…"
 * indicator.
 */
export function CompactionMarker() {
  return (
    <div className="flex w-full items-center gap-3 py-1 text-13 leading-5 text-muted-foreground">
      <span className="h-px flex-1 bg-border" />
      <span>Conversation compacted</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

interface RoutingDecisionChipProps {
  model: string;
  applied: boolean;
  rationale: string;
}

/**
 * Muted inline chip announcing the intelligent model router's pick at
 * the start of a turn.
 */
export function RoutingDecisionChip({ model, applied, rationale }: RoutingDecisionChipProps) {
  const short = shortModelName(model);
  const lead = applied ? short : `would have picked ${short}`;
  const summary = `Intelligent model router · ${lead}`;
  return (
    <div
      className="my-1 flex flex-col items-center gap-0.5 text-muted-foreground text-xs"
      data-testid="routing-decision-chip"
      data-applied={applied ? "true" : "false"}
      title={rationale || summary}
    >
      <span className="flex items-center gap-1.5">
        <BrainCircuitIcon className="size-3 shrink-0" />
        <span>
          Intelligent model router{" · "}
          {!applied && <span>would have picked </span>}
          <span className="font-medium text-foreground">{short}</span>
        </span>
      </span>
      {rationale ? <span className="text-muted-foreground/70">{rationale}</span> : null}
    </div>
  );
}

interface RoutingDecisionCardProps {
  model: string;
  applied: boolean;
  rationale: string;
  /** Sub-agent name when this card is shown in the parent session. */
  agent?: string;
}

/**
 * Collapsible card announcing the intelligent model router's session-level
 * pick. Mirrors the SmartRoutingCard style: same container, model pill,
 * rationale, and expandable raw verdict JSON.
 *
 * Shown in place of the muted chip when auto-routing fires at turn start
 * because the agent spec has no explicit model. When `agent` is provided
 * the card is being shown in the parent (orchestrator) session for a child
 * session's routing decision — the agent name is shown as the row label.
 */
export function RoutingDecisionCard({
  model,
  applied,
  rationale,
  agent,
}: RoutingDecisionCardProps) {
  const short = shortModelName(model);
  const rowLabel = agent && agent.length > 0 ? agent : "Session";
  const prettyOutput = useMemo(
    () => JSON.stringify({ model, applied, rationale, ...(agent ? { agent } : {}) }, null, 2),
    [model, applied, rationale, agent],
  );
  return (
    <Collapsible
      defaultOpen={false}
      className={cn(
        "group not-prose my-1 flex flex-col gap-1.5 px-3 py-2",
        COMPACT_TRANSCRIPT_CARD_CLASS,
        TOOL_SURFACE_WIDTH_CLASS,
      )}
      data-testid="routing-decision-card"
      data-applied={applied ? "true" : "false"}
    >
      <div className={cn("flex items-center gap-1.5", TRANSCRIPT_CARD_TITLE_CLASS)}>
        <BrainCircuitIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium">Intelligent routing</span>
        <span className="text-muted-foreground">{applied ? "· applied" : "· advisory"}</span>
        <CollapsibleTrigger
          className="ml-auto cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
          aria-label="Show raw routing verdict"
          data-testid="routing-decision-raw-toggle"
        >
          <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
        </CollapsibleTrigger>
      </div>
      <div className={cn("flex items-center gap-2", TRANSCRIPT_CARD_BODY_CLASS)}>
        <span className="min-w-0 truncate font-mono text-foreground">{rowLabel}</span>
        <span
          className={cn(
            "ml-auto shrink-0 inline-flex items-center whitespace-nowrap rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono font-medium text-foreground",
            TRANSCRIPT_CARD_META_CLASS,
          )}
        >
          {short}
        </span>
      </div>
      {rationale.length > 0 && (
        <p className={cn("text-muted-foreground", TRANSCRIPT_CARD_BODY_CLASS)}>{rationale}</p>
      )}
      <CollapsibleContent className="data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        <CodeBlock code={prettyOutput} language="json">
          <CodeBlockHeader>
            <CodeBlockTitle className="min-w-0">
              <span className="truncate font-medium uppercase tracking-wide">Verdict</span>
            </CodeBlockTitle>
          </CodeBlockHeader>
        </CodeBlock>
      </CollapsibleContent>
    </Collapsible>
  );
}
