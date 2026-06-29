// Inline status indicators for non-tool, non-text, non-reasoning blocks.
// Each is small enough to live in one file.
//
// - ErrorBanner: destructive Alert with `[source]` + code + message.
// - RetryIndicator: muted one-liner about an in-flight retry.
// - CompactionMarker: permanent marker shown after compaction completes.
//   The in-progress state renders as a Shimmer in ChatPage, mirroring
//   the "Working…" indicator.

import {
  AlertCircleIcon,
  RotateCcwIcon,
  ShieldXIcon,
  ShrinkIcon,
  WaypointsIcon,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { shortModelName } from "@/components/CostRoutingControl";

interface ErrorBannerProps {
  message: string;
  source: string;
  code: string;
}

/**
 * Loud destructive banner for `error` blocks. Falls back to `code` when
 * `message` is empty (matches the reducer's intent — never show a blank
 * panel even when the LLM error payload omits the message).
 */
export function ErrorBanner({ message, source, code }: ErrorBannerProps) {
  const display = message || code || "Unknown error";
  return (
    <Alert
      variant="destructive"
      className="min-w-0 max-w-full overflow-hidden has-[>svg]:grid-cols-[auto_minmax(0,1fr)]"
    >
      <AlertCircleIcon />
      <AlertTitle className="min-w-0 break-words [overflow-wrap:anywhere]">
        Error{source ? ` · ${source}` : ""}
        {code && message ? ` · ${code}` : ""}
      </AlertTitle>
      <AlertDescription className="min-w-0 max-w-full overflow-hidden">
        <span className="block max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere] [text-wrap:wrap]">
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
    <Alert>
      <ShieldXIcon />
      <AlertTitle>Blocked by policy{phase ? ` · ${phase}` : ""}</AlertTitle>
      <AlertDescription>{reason}</AlertDescription>
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
    <div className="flex items-center gap-2 text-muted-foreground text-xs italic">
      <ShrinkIcon className="size-3" />
      <span>Conversation compacted</span>
    </div>
  );
}

interface RoutingDecisionChipProps {
  model: string;
  tier: "cheap" | "medium" | "expensive";
  applied: boolean;
  rationale: string;
}

/**
 * Muted inline chip announcing the intelligent model router's pick at
 * the start of a turn. Precision-grayscale, tiny waypoints glyph, a primary
 * line `Intelligent model router · <short model> (<tier>)`, and the
 * router's rationale as a muted second line (also the `title` for hover).
 * When the verdict was not applied (advise/shadow or a user model pin
 * won), the line reads "would have picked" instead of naming the active
 * model — visible without hovering anything, surviving reload.
 *
 * @param model Model id the router chose, e.g. `databricks-claude-opus-4-8`.
 * @param tier Difficulty tier the router assigned.
 * @param applied `true` when the brain ran on `model` this turn.
 * @param rationale One-line router explanation; hidden when empty.
 */
export function RoutingDecisionChip({ model, tier, applied, rationale }: RoutingDecisionChipProps) {
  const short = shortModelName(model);
  const lead = applied ? short : `would have picked ${short}`;
  const summary = `Intelligent model router · ${lead} (${tier})`;
  return (
    <div
      className="my-1 flex flex-col items-center gap-0.5 text-muted-foreground text-xs"
      data-testid="routing-decision-chip"
      data-applied={applied ? "true" : "false"}
      title={rationale || summary}
    >
      <span className="flex items-center gap-1.5">
        <WaypointsIcon className="size-3 shrink-0" />
        <span>
          Intelligent model router{" · "}
          {!applied && <span>would have picked </span>}
          <span className="font-medium text-foreground">{short}</span>
          <span className="text-muted-foreground/80">{` (${tier})`}</span>
        </span>
      </span>
      {rationale ? <span className="text-muted-foreground/70">{rationale}</span> : null}
    </div>
  );
}
