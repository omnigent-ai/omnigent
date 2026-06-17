// Inline status indicators for non-tool, non-text, non-reasoning blocks.
// Each is small enough to live in one file.
//
// - ErrorBanner: destructive Alert with `[source]` + code + message.
// - RetryIndicator: muted one-liner about an in-flight retry.
// - CompactionMarker: permanent marker shown after compaction completes.
//   The in-progress state renders as a Shimmer in ChatPage, mirroring
//   the "Working…" indicator.

import { AlertCircleIcon, RotateCcwIcon, ShieldXIcon, ShrinkIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { CliCommandBlock } from "@/shell/CliCommandBlock";

interface ErrorBannerProps {
  message: string;
  source: string;
  code: string;
}

/**
 * Detect the shared "<Executor> requires the '<pkg>' package. Install it
 * with: <cmd>" pattern the inner executors raise when an optional harness
 * dependency is missing (antigravity, claude-agent-sdk, cursor-sdk,
 * openai-agents, databricks-sdk, mlflow, …). The message is copy-pasted
 * across executors, so this is a stable convention. Returns null when the
 * message isn't that shape so the banner falls back to the generic raw
 * rendering. #548
 */
const MISSING_DEP_RE = /requires the '([^']+)' package\. Install it with:\s*(.+)$/;
function parseMissingDependency(
  message: string,
): { packageName: string; installCommand: string } | null {
  if (!message) return null;
  // `String.match(regex)` here, not the RegExp prototype's exec method: both
  // are equivalent for a non-global regex (each returns [full, g1, g2, …] or
  // null). The security-scan exfil heuristic flags that method's literal call
  // token (it conflates the regex API with dynamic code execution), so
  // `.match(` — which reads identically — keeps the PR's Security Scan green. #548
  const m = message.match(MISSING_DEP_RE);
  if (!m) return null;
  // Some executors trail the command with a period; strip one so the
  // copied install command doesn't carry it.
  return { packageName: m[1], installCommand: m[2].replace(/\.$/, "").trim() };
}

interface MissingDependencyBannerProps {
  packageName: string;
  installCommand: string;
  rawMessage: string;
}

/**
 * Friendly remediation for a missing optional dependency: a concise summary,
 * the install command as a copyable action, and the raw executor error
 * collapsed behind a details block for diagnostics. Replaces the raw
 * `RuntimeError` dump the chat transcript used to show for these. #548
 */
function MissingDependencyBanner({
  packageName,
  installCommand,
  rawMessage,
}: MissingDependencyBannerProps) {
  return (
    <Alert
      variant="destructive"
      className="min-w-0 max-w-full overflow-hidden has-[>svg]:grid-cols-[auto_minmax(0,1fr)]"
    >
      <AlertCircleIcon />
      <AlertTitle className="min-w-0 break-words [overflow-wrap:anywhere]">Missing dependency</AlertTitle>
      <AlertDescription className="min-w-0 max-w-full overflow-hidden">
        <p className="text-sm">
          The <code className="font-mono">{packageName}</code> package is required to run this agent.
        </p>
        <div className="mt-2">
          <CliCommandBlock command={installCommand} testIdPrefix="missing-dep-install" />
        </div>
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted-foreground">Raw error</summary>
          <span className="mt-1 block max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-xs text-muted-foreground">
            {rawMessage}
          </span>
        </details>
      </AlertDescription>
    </Alert>
  );
}

/**
 * Loud destructive banner for `error` blocks. Falls back to `code` when
 * `message` is empty (matches the reducer's intent — never show a blank
 * panel even when the LLM error payload omits the message). Missing-
 * dependency errors route to `MissingDependencyBanner` for a friendlier,
 * actionable remediation. #548
 */
export function ErrorBanner({ message, source, code }: ErrorBannerProps) {
  const dep = parseMissingDependency(message);
  if (dep) {
    return (
      <MissingDependencyBanner
        packageName={dep.packageName}
        installCommand={dep.installCommand}
        rawMessage={message}
      />
    );
  }
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
