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
  BrainCircuitIcon,
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  RotateCcwIcon,
  ShieldXIcon,
  ShrinkIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { DatabricksIcon } from "@/components/icons/DatabricksIcon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { shortModelName } from "@/components/CostRoutingControl";
import { copyText } from "@/lib/clipboard";
import {
  type RoutingDecisionExtras,
  harnessDisplayLabel,
  subagentScopeLabel,
} from "@/lib/routingDecision";
import { cn } from "@/lib/utils";
import { TOOL_SURFACE_WIDTH_CLASS } from "./toolSurface";

interface ErrorBannerProps {
  message: string;
  source: string;
  code: string;
  /** Friendly headline for a classified failure, e.g. "Claude Code can't run as root". */
  title?: string;
  /** One/two-sentence explanation of why it failed. Paired with `title`. */
  cause?: string;
  /** Concrete next step to fix it, e.g. a command to run. */
  remediation?: string;
  /** Reconnect the existing session without replaying or duplicating user input. */
  onRetry?: () => Promise<void>;
}

/**
 * One-line English descriptions for known failure `code`s, so an
 * unclassified failure (no `title`/`cause` from the runner) still reads as a
 * sentence instead of a raw enum. Mirrors the server-side
 * `describe_failure_code` table (omnigent/runner/launch_failure.py); keep the
 * two in sync.
 */
const FAILURE_CODE_DESCRIPTIONS: Record<string, string> = {
  required_terminal_exited:
    "The agent's terminal exited unexpectedly, so the session can't continue.",
  terminal_launch_failed: "The agent's terminal couldn't be started on the host.",
  runner_error: "Something went wrong setting up the turn on the host.",
  runner_disconnected: "The connection to the host dropped unexpectedly.",
  connection_error: "The connection to the agent dropped mid-turn.",
  context_length_exceeded: "The conversation grew past the model's context window.",
  executor_error: "The agent runtime hit an error while running the turn.",
  workspace_missing: "The session workspace no longer exists on the host.",
};

const RETRYABLE_ERROR_CODES = new Set([
  "required_terminal_exited",
  "terminal_launch_failed",
  "runner_disconnected",
  "runner_failed_to_start",
  "runner_unavailable",
]);

interface ParsedErrorMessage {
  message: string;
  terminal: string | null;
  lastOutput: string | null;
}

const DIAGNOSTICS_HEADING = /^(?:terminal|lifecycle) diagnostics:\s*$/i;
const LAST_OUTPUT_HEADING = /^last captured (?:terminal )?output:\s*(.*)$/i;
const UNAVAILABLE_OUTPUT =
  /^unavailable(?:[.!]|\. The process exited before Omnigent captured a pane snapshot\.)?$/i;

function trimBlankBoundaryLines(lines: string[]): string | null {
  let start = 0;
  let end = lines.length;
  while (start < end && lines[start]!.trim() === "") start += 1;
  while (end > start && lines[end - 1]!.trim() === "") end -= 1;
  return start < end ? lines.slice(start, end).join("\n") : null;
}

function parseErrorMessage(rawMessage: string): ParsedErrorMessage {
  const lines = rawMessage.replace(/\r\n?/g, "\n").split("\n");
  const diagnosticsIndex = lines.findIndex((line) => DIAGNOSTICS_HEADING.test(line.trim()));
  const searchStart = diagnosticsIndex >= 0 ? diagnosticsIndex + 1 : 0;
  const lastOutputIndex = lines.findIndex(
    (line, index) => index >= searchStart && LAST_OUTPUT_HEADING.test(line.trim()),
  );

  if (diagnosticsIndex < 0 && lastOutputIndex < 0) {
    return { message: rawMessage.trim(), terminal: null, lastOutput: null };
  }

  const messageEnd = diagnosticsIndex >= 0 ? diagnosticsIndex : lastOutputIndex;
  const message = lines.slice(0, messageEnd).join("\n").trim();
  const terminal =
    diagnosticsIndex >= 0
      ? trimBlankBoundaryLines(
          lines.slice(diagnosticsIndex + 1, lastOutputIndex >= 0 ? lastOutputIndex : undefined),
        )
      : null;
  let lastOutput: string | null = null;
  if (lastOutputIndex >= 0) {
    const headingMatch = lines[lastOutputIndex]!.trim().match(LAST_OUTPUT_HEADING);
    const headingValue = headingMatch?.[1]?.trim() ?? "";
    lastOutput = trimBlankBoundaryLines([headingValue, ...lines.slice(lastOutputIndex + 1)]);
    if (lastOutput && UNAVAILABLE_OUTPUT.test(lastOutput.trim())) lastOutput = null;
  }
  return { message, terminal, lastOutput };
}

/**
 * Loud destructive banner for `error` blocks.
 *
 * When the runner classified the failure it passes `title` / `cause` /
 * `remediation`, and the banner renders a clear card: headline, plain-English
 * cause, a "Try this" remediation line, and the raw diagnostics folded into a
 * collapsible. Otherwise it falls back to a code→sentence map (so even an
 * unclassified failure reads as English) and finally to the raw message/code —
 * never a blank panel.
 */
export function ErrorBanner({
  message,
  source,
  code,
  title,
  cause,
  remediation,
  onRetry,
}: ErrorBannerProps) {
  const headline = title || FAILURE_CODE_DESCRIPTIONS[code] || "Something went wrong";
  const parsed = useMemo(() => parseErrorMessage(message), [message]);
  const messageText = useMemo(() => {
    const parts: string[] = [];
    if (cause) parts.push(cause);
    if (remediation) parts.push(`Try this: ${remediation}`);
    if (!cause && parsed.message && parsed.message !== headline) parts.push(parsed.message);
    if (parts.length === 0) parts.push(parsed.message || code || headline);
    return parts.join("\n\n");
  }, [cause, code, headline, parsed.message, remediation]);
  const diagnostics = useMemo(
    () =>
      [
        parsed.terminal ? { id: "terminal", label: "Terminal", content: parsed.terminal } : null,
        parsed.lastOutput
          ? { id: "output", label: "Last captured output", content: parsed.lastOutput }
          : null,
      ].filter((item): item is { id: string; label: string; content: string } => item !== null),
    [parsed.lastOutput, parsed.terminal],
  );
  const [expanded, setExpanded] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [activeDiagnostics, setActiveDiagnostics] = useState(diagnostics[0]?.id ?? "terminal");
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [headerHovered, setHeaderHovered] = useState(false);
  const [headerFocusVisible, setHeaderFocusVisible] = useState(false);
  const copyResetRef = useRef<number>(0);
  const retryInFlightRef = useRef(false);
  const headerPointerDownRef = useRef(false);
  const dismissButtonRef = useRef<HTMLButtonElement>(null);
  const messageId = useId();
  const diagnosticsId = useId();
  const retryable = onRetry !== undefined && RETRYABLE_ERROR_CODES.has(code);
  const showDisclosureIcon = expanded || headerHovered || headerFocusVisible;

  useEffect(() => () => window.clearTimeout(copyResetRef.current), []);
  useEffect(() => {
    if (!diagnostics.some((item) => item.id === activeDiagnostics)) {
      setActiveDiagnostics(diagnostics[0]?.id ?? "terminal");
    }
  }, [activeDiagnostics, diagnostics]);
  useEffect(() => {
    if (retryError && !retrying) dismissButtonRef.current?.focus();
  }, [retryError, retrying]);

  if (dismissed) return null;

  if (retrying) {
    return (
      <div
        data-testid="error-reconnecting"
        className={cn(
          TOOL_SURFACE_WIDTH_CLASS,
          "relative flex min-h-14 items-center justify-center py-2",
        )}
      >
        <span
          aria-hidden="true"
          className="absolute inset-x-0 top-1/2 border-border border-t border-dashed"
        />
        <Badge
          variant="outline"
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className="relative z-10 h-auto rounded-xl border-border bg-background px-4 py-2 text-sm font-normal text-muted-foreground shadow-xs"
        >
          Reconnecting
        </Badge>
      </div>
    );
  }

  const copy = async (target: string, value: string) => {
    await copyText(value);
    setCopiedTarget(target);
    window.clearTimeout(copyResetRef.current);
    copyResetRef.current = window.setTimeout(() => setCopiedTarget(null), 2000);
  };

  const retry = async () => {
    if (!onRetry || retryInFlightRef.current) return;
    retryInFlightRef.current = true;
    setRetrying(true);
    setRetryError(null);
    try {
      await onRetry();
      setDismissed(true);
    } catch (error) {
      retryInFlightRef.current = false;
      setRetryError(`Retry failed: ${error instanceof Error ? error.message : String(error)}`);
      setRetrying(false);
    }
  };

  return (
    <Alert
      variant="destructive"
      className={cn(
        TOOL_SURFACE_WIDTH_CLASS,
        "block overflow-hidden rounded-2xl border-destructive/30 bg-destructive/[0.045] p-4 text-destructive sm:p-5",
      )}
    >
      <div className="min-w-0 max-w-full">
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_auto] items-start gap-x-2">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={expanded ? messageId : undefined}
            onClick={() => setExpanded((value) => !value)}
            onMouseEnter={() => setHeaderHovered(true)}
            onMouseLeave={() => setHeaderHovered(false)}
            onPointerDown={() => {
              headerPointerDownRef.current = true;
              setHeaderFocusVisible(false);
            }}
            onPointerUp={() => {
              headerPointerDownRef.current = false;
            }}
            onPointerCancel={() => {
              headerPointerDownRef.current = false;
            }}
            onFocus={(event) => {
              setHeaderFocusVisible(
                !headerPointerDownRef.current && event.currentTarget.matches(":focus-visible"),
              );
            }}
            onKeyDown={(event) => {
              setHeaderFocusVisible(event.currentTarget.matches(":focus-visible"));
            }}
            onBlur={() => {
              headerPointerDownRef.current = false;
              setHeaderFocusVisible(false);
            }}
            className="group/header -m-1 grid min-w-0 cursor-pointer grid-cols-[1.25rem_minmax(0,1fr)] items-start gap-x-3 rounded-lg p-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          >
            <span
              data-testid="error-leading-slot"
              className="mt-0.5 flex size-5 shrink-0 items-center justify-center"
            >
              {showDisclosureIcon ? (
                <ChevronRightIcon
                  data-testid="error-disclosure-icon"
                  className={cn(
                    "size-4 text-foreground transition-transform duration-150 animate-in fade-in",
                    expanded && "rotate-90",
                  )}
                  aria-hidden="true"
                />
              ) : (
                <AlertCircleIcon
                  data-testid="error-status-icon"
                  className="size-4 animate-in fade-in duration-150"
                  aria-hidden="true"
                />
              )}
            </span>
            <AlertTitle className="min-w-0 pr-1 text-sm font-semibold leading-6 text-destructive sm:text-base">
              <span
                data-testid="error-headline"
                className={cn(
                  "block min-w-0",
                  expanded ? "break-words [overflow-wrap:anywhere]" : "truncate",
                )}
              >
                {headline}
                {source ? (
                  <span className="font-normal text-destructive/75"> · {source}</span>
                ) : null}
              </span>
            </AlertTitle>
          </button>
          {retryable ? (
            <Button
              type="button"
              variant="ghost"
              size="xs"
              onClick={() => void retry()}
              className="mt-0.5 shrink-0 text-muted-foreground hover:text-foreground"
            >
              <RotateCcwIcon aria-hidden="true" />
              Retry
            </Button>
          ) : null}
          <Button
            ref={dismissButtonRef}
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Dismiss error"
            onClick={() => setDismissed(true)}
            className="mt-0.5 shrink-0 text-muted-foreground hover:text-foreground"
          >
            <XIcon aria-hidden="true" />
          </Button>
        </div>
        {retryError ? (
          <div
            role="status"
            aria-live="assertive"
            aria-atomic="true"
            className="mt-2 break-words pl-8 text-sm text-destructive [overflow-wrap:anywhere]"
          >
            {retryError}
          </div>
        ) : null}
        {expanded ? (
          <AlertDescription
            id={messageId}
            className="mt-6 min-w-0 max-w-full overflow-hidden text-foreground [text-wrap:wrap]"
          >
            <section aria-labelledby={`${messageId}-label`} className="min-w-0 space-y-2">
              <h4 id={`${messageId}-label`} className="text-sm font-medium text-muted-foreground">
                Message
              </h4>
              <div className="relative min-w-0">
                <div
                  data-testid="error-message-content"
                  className="max-w-full min-w-0 whitespace-pre-wrap break-words pr-10 font-mono text-sm leading-6 text-foreground [overflow-wrap:anywhere] [text-wrap:wrap]"
                >
                  {messageText}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="absolute top-0 right-0 text-muted-foreground hover:text-foreground"
                  aria-label={copiedTarget === "message" ? "Message copied" : "Copy message"}
                  onClick={() => void copy("message", messageText)}
                >
                  {copiedTarget === "message" ? (
                    <CheckIcon aria-hidden="true" />
                  ) : (
                    <CopyIcon aria-hidden="true" />
                  )}
                </Button>
              </div>
            </section>
            {diagnostics.length > 0 ? (
              <Collapsible
                open={diagnosticsOpen}
                onOpenChange={setDiagnosticsOpen}
                className="mt-6 border-t border-destructive/15 pt-4"
              >
                <CollapsibleTrigger asChild>
                  <button
                    type="button"
                    aria-controls={diagnosticsOpen ? diagnosticsId : undefined}
                    className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-md py-1 text-left text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                  >
                    <span>View diagnostics</span>
                    <ChevronRightIcon
                      className={cn(
                        "size-4 shrink-0 transition-transform",
                        diagnosticsOpen && "rotate-90",
                      )}
                      aria-hidden="true"
                    />
                  </button>
                </CollapsibleTrigger>
                <CollapsibleContent id={diagnosticsId} className="min-w-0 pt-4">
                  <Tabs value={activeDiagnostics} onValueChange={setActiveDiagnostics}>
                    {diagnostics.length > 1 ? (
                      <TabsList
                        variant="line"
                        aria-label="Diagnostic sections"
                        className="h-auto gap-5 p-0"
                      >
                        {diagnostics.map((item) => (
                          <TabsTrigger
                            key={item.id}
                            value={item.id}
                            className="h-auto flex-none rounded-none px-0 pt-0 pb-2 text-sm after:bottom-0"
                          >
                            {item.label}
                          </TabsTrigger>
                        ))}
                      </TabsList>
                    ) : null}
                    {diagnostics.map((item) => (
                      <TabsContent key={item.id} value={item.id} className="min-w-0">
                        <div className="relative mt-1 min-w-0 rounded-xl bg-zinc-950 p-4 pr-12 text-zinc-100 shadow-inner">
                          <pre
                            data-testid="error-diagnostics-content"
                            className="max-h-64 min-w-0 overflow-auto whitespace-pre-wrap break-words font-mono text-sm leading-6 text-zinc-100 [overflow-wrap:anywhere]"
                          >
                            {item.content}
                          </pre>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon-xs"
                            className="absolute top-3 right-3 text-zinc-300 hover:bg-white/10 hover:text-white"
                            aria-label={
                              copiedTarget === item.id
                                ? `${item.label} copied`
                                : `Copy ${item.label.toLowerCase()}`
                            }
                            onClick={() => void copy(item.id, item.content)}
                          >
                            {copiedTarget === item.id ? (
                              <CheckIcon aria-hidden="true" />
                            ) : (
                              <CopyIcon aria-hidden="true" />
                            )}
                          </Button>
                        </div>
                      </TabsContent>
                    ))}
                  </Tabs>
                </CollapsibleContent>
              </Collapsible>
            ) : null}
          </AlertDescription>
        ) : null}
      </div>
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
    <div className="flex items-center gap-2 text-muted-foreground text-sm">
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
    <div className="flex items-center gap-2 text-muted-foreground text-sm italic">
      <ShrinkIcon className="size-3" />
      <span>Conversation compacted</span>
    </div>
  );
}

/**
 * Short raw-pick name when the router's own vocabulary pick differs from the
 * model actually applied (an unservable arm mapped to a servable id), else
 * `null` — the common case where both names collapse to the same tier.
 */
function rawPickName(model: string, rawModel: string | null | undefined): string | null {
  if (!rawModel) return null;
  const raw = shortModelName(rawModel);
  return raw === shortModelName(model) ? null : raw;
}

/**
 * Short name of the model the spawn asked for, when Smart Routing picked a
 * different one. `null` when nothing was attempted or both names collapse to
 * the same tier — a struck-through name identical to the pill reads as a bug.
 */
function attemptedPickName(
  model: string,
  attemptedOverride: string | null | undefined,
): string | null {
  if (!attemptedOverride) return null;
  const attempted = shortModelName(attemptedOverride);
  return attempted === shortModelName(model) ? null : attempted;
}

interface RoutingDecisionCardProps {
  model: string;
  applied: boolean;
  rationale: string;
  /** Sub-agent name when this card is shown in the parent session. */
  agent?: string;
  /** Routing identity (harness, scope, decision id …); absent on legacy rows. */
  routing?: RoutingDecisionExtras;
}

/**
 * Collapsible card announcing smart routing's session-level
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
  routing,
}: RoutingDecisionCardProps) {
  const { harness, scope, decisionId, rawModel, attemptedOverride, routerSource } = routing ?? {};
  const short = shortModelName(model);
  const rawShort = rawPickName(model, rawModel);
  const attemptedShort = attemptedPickName(model, attemptedOverride);
  const scopeLabel = subagentScopeLabel(scope, agent);
  const harnessLabel = harnessDisplayLabel(harness);
  const rowLabel = agent && agent.length > 0 ? agent : "Session";
  const prettyOutput = useMemo(
    () =>
      JSON.stringify(
        {
          model,
          applied,
          rationale,
          ...(agent ? { agent } : {}),
          ...(harness ? { harness } : {}),
          ...(scope ? { scope } : {}),
          ...(decisionId ? { decision_id: decisionId } : {}),
          ...(rawModel ? { raw_model: rawModel } : {}),
          ...(attemptedOverride ? { attempted_override: attemptedOverride } : {}),
          ...(routerSource ? { router_source: routerSource } : {}),
        },
        null,
        2,
      ),
    [
      model,
      applied,
      rationale,
      agent,
      harness,
      scope,
      decisionId,
      rawModel,
      attemptedOverride,
      routerSource,
    ],
  );
  return (
    <Collapsible
      defaultOpen={false}
      className={cn(
        "group not-prose my-1 flex flex-col gap-1.5 rounded-md border border-border bg-muted/30 px-3 py-2",
        TOOL_SURFACE_WIDTH_CLASS,
      )}
      data-testid="routing-decision-card"
      data-applied={applied ? "true" : "false"}
    >
      <div className="flex items-center gap-1.5 text-sm">
        <BrainCircuitIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium">Smart routing</span>
        {routerSource === "databricks-aigw" ? (
          <span
            className="text-muted-foreground"
            title="Routed by the Databricks AI Gateway"
            aria-label="Routed by the Databricks AI Gateway"
            role="img"
            data-testid="routing-decision-source-databricks"
          >
            <DatabricksIcon className="size-3 shrink-0" />
          </span>
        ) : null}
        <span className="text-muted-foreground">{applied ? "· applied" : "· advisory"}</span>
        {harnessLabel ? (
          <span className="text-muted-foreground" data-testid="routing-decision-harness">
            · {harnessLabel}
          </span>
        ) : null}
        {scopeLabel ? (
          <span className="text-muted-foreground" data-testid="routing-decision-scope">
            · {scopeLabel}
          </span>
        ) : null}
        <CollapsibleTrigger
          className="ml-auto cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
          aria-label="Show raw routing verdict"
          data-testid="routing-decision-raw-toggle"
        >
          <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
        </CollapsibleTrigger>
      </div>
      <div className="flex items-center gap-2 text-sm">
        <span className="min-w-0 truncate font-mono text-foreground">{rowLabel}</span>
        {attemptedShort ? (
          // The spawn named its own model and the router picked another — the
          // substitution is the whole point of the row, so it shows at a glance.
          <span
            className="ml-auto shrink-0 whitespace-nowrap font-mono text-[10px] text-muted-foreground line-through"
            data-testid="routing-decision-attempted-override"
            title="The spawn asked for this model; Smart Routing picked the one on the right."
          >
            {attemptedShort}
          </span>
        ) : null}
        {rawShort ? (
          // Router vocabulary pick that had to be mapped to a servable id —
          // show what the router said next to what actually ran.
          <span
            className={cn(
              "shrink-0 whitespace-nowrap font-mono text-[10px] text-muted-foreground",
              !attemptedShort && "ml-auto",
            )}
            data-testid="routing-decision-raw-model"
          >
            {rawShort} →
          </span>
        ) : null}
        <span
          className={cn(
            "shrink-0 inline-flex items-center whitespace-nowrap rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] font-medium leading-none text-foreground",
            !rawShort && !attemptedShort && "ml-auto",
          )}
        >
          {short}
        </span>
      </div>
      {rationale.length > 0 && (
        <p className="text-sm leading-snug text-muted-foreground">{rationale}</p>
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
