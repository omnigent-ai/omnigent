import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useHosts } from "@/hooks/useHosts";
import { Link } from "@/lib/routing";
import { HostLabel } from "./HostLabel";
import { CliCommandBlock, renderTextWithInlineCode } from "./CliCommandBlock";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ApiError,
  importErrorFromException,
  importLocalSessions,
  type ImportErrorInfo,
  type ImportFailureRef,
  type ImportedSessionRef,
  type ImportProgress,
  type ImportSourceSelector,
  type LocalImportResult,
} from "@/lib/sessionsApi";

// Harnesses the import endpoint accepts, with human labels for the picker.
// "all" reads every supported harness on the host in one batch.
const SOURCES: { value: ImportSourceSelector; label: string }[] = [
  { value: "all", label: "All harnesses" },
  { value: "claude", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "opencode", label: "OpenCode" },
  { value: "pi", label: "Pi" },
  { value: "qwen", label: "Qwen" },
  { value: "kiro", label: "Kiro" },
  { value: "kimi", label: "Kimi" },
];

const LIMITS = [25, 50, 100];
type ImportMode = "recent" | "session";

// Import codes that mean the picker's view of the host is stale: refetch the
// host list so an offline machine drops out instead of failing again.
const HOST_STALE_IMPORT_CODES = new Set([
  "host_offline",
  "host_unreachable",
  "host_disconnected",
  "host_unresponsive",
]);

/** Whether an import error should refresh the host list. */
function hostListLooksStale(error: ImportErrorInfo, thrown: unknown): boolean {
  if (error.code !== null) return HOST_STALE_IMPORT_CODES.has(error.code);
  // A server that predates import codes sends its "host is offline" as a bare 409.
  return thrown instanceof ApiError && thrown.status === 409;
}

/**
 * "Imported 12 · 3 already imported · 2 failed" (zero parts omitted), or
 * null when there is nothing to report beside an error. A run that stopped
 * early (`complete: false`) never reads "Nothing new to import": it leads with
 * what it imported and says it stopped, e.g. "Imported 0 · 10 already
 * imported · stopped early".
 */
function importSummary(result: LocalImportResult): string | null {
  const stoppedEarly = !result.complete || result.error !== null;
  const parts = [
    result.imported > 0 ? `Imported ${result.imported}` : null,
    result.alreadyImported > 0 ? `${result.alreadyImported} already imported` : null,
    result.failed > 0 ? `${result.failed} failed` : null,
  ].filter((p): p is string => p !== null);
  if (stoppedEarly) {
    if (parts.length === 0) return null;
    if (result.imported === 0) parts.unshift("Imported 0");
    return [...parts, "stopped early"].join(" · ");
  }
  if (result.imported === 0 && result.failed === 0) {
    if (result.alreadyImported > 0) return `Nothing new to import · ${parts.join(" · ")}`;
    return result.error === null ? "No sessions to import." : null;
  }
  return parts.join(" · ");
}

/** "Importing 7 of 20…" / "Importing… 7 so far" / "Importing…". */
function progressText(progress: ImportProgress | null, streamedCount: number): string {
  if (progress !== null && progress.total !== null) {
    return `Importing ${Math.min(progress.done, progress.total)} of ${progress.total}…`;
  }
  const count = Math.max(progress?.done ?? 0, streamedCount);
  return count > 0 ? `Importing… ${count} so far` : "Importing…";
}

function sourceLabel(source: string | null): string | null {
  if (source === null) return null;
  return SOURCES.find((s) => s.value === source)?.label ?? source;
}

/** Code + error id under a collapsed "Details" disclosure; nothing when neither is known. */
function ImportErrorDetails({
  code,
  errorId,
  testId,
}: {
  code: string | null;
  errorId: string | null;
  testId: string;
}) {
  if (code === null && errorId === null) return null;
  return (
    <details className="text-xs text-muted-foreground" data-testid={testId}>
      <summary className="cursor-pointer select-none hover:text-foreground">Details</summary>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono">
        {code !== null && (
          <>
            <dt>code</dt>
            <dd>{code}</dd>
          </>
        )}
        {errorId !== null && (
          <>
            <dt>error id</dt>
            <dd className="break-all select-all">{errorId}</dd>
          </>
        )}
      </dl>
    </details>
  );
}

/** One session that couldn't be imported: reason, which session, and details. */
function ImportFailureRow({ failure }: { failure: ImportFailureRef }) {
  const harness = sourceLabel(failure.source);
  return (
    <li
      className="flex flex-col gap-0.5 text-sm text-muted-foreground"
      data-testid="import-failure-item"
    >
      <span className="text-destructive">{failure.reason}</span>
      {(failure.externalSessionId !== null || harness !== null) && (
        <span className="font-mono text-xs">
          {[harness, failure.externalSessionId].filter((p) => p !== null).join(" · ")}
        </span>
      )}
      <ImportErrorDetails
        code={failure.code}
        errorId={failure.errorId}
        testId="import-failure-details"
      />
    </li>
  );
}

/** Why the import as a whole stopped, with any fix commands to copy. */
function ImportErrorBanner({ error }: { error: ImportErrorInfo }) {
  return (
    <Alert variant="destructive" data-testid="import-error" data-import-code={error.code ?? ""}>
      <AlertDescription className="flex flex-col gap-2 text-destructive">
        <div data-testid="import-error-message">{renderTextWithInlineCode(error.message)}</div>
        {error.fixCommands.length > 0 && (
          <div className="flex flex-col gap-1.5" data-testid="import-fix-commands">
            {error.fixCommands.map((fix, i) => (
              <div key={`${fix.label ?? ""}:${fix.command}`} className="flex flex-col gap-0.5">
                {fix.label !== null && (
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid={`import-fix-${i}-label`}
                  >
                    {fix.label}
                  </span>
                )}
                <CliCommandBlock command={fix.command} testIdPrefix={`import-fix-${i}`} />
              </div>
            ))}
          </div>
        )}
        <ImportErrorDetails
          code={error.code}
          errorId={error.errorId}
          testId="import-error-details"
        />
      </AlertDescription>
    </Alert>
  );
}

/**
 * Inline (non-modal) import UI for the Settings "Import sessions" section.
 * Imports recent transcripts or one known harness session through the chosen
 * host, then refreshes the sidebar. Already-imported sessions are skipped
 * server-side; the result links each newly imported session.
 */
export function ImportSessionsPanel() {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const [hostId, setHostId] = useState<string | null>(null);
  const [source, setSource] = useState<ImportSourceSelector>("all");
  const [limit, setLimit] = useState(25);
  const [mode, setMode] = useState<ImportMode>("recent");
  const [sessionId, setSessionId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Sessions appended as their frames stream in, so the list fills live rather
  // than appearing all at once when the import finishes.
  const [streamed, setStreamed] = useState<ImportedSessionRef[]>([]);
  const [progress, setProgress] = useState<ImportProgress | null>(null);
  const [result, setResult] = useState<LocalImportResult | null>(null);
  // Why the whole import stopped: a stream `error`, a dropped connection, or a
  // pre-stream HTTP error. Shown alongside whatever partial tally arrived.
  const [error, setError] = useState<ImportErrorInfo | null>(null);

  // Default to the caller's current online machine — the transcripts are read
  // on the host, so there's nothing to import without one.
  useEffect(() => {
    if (hostId === null && onlineHosts.length > 0) {
      setHostId(onlineHosts[0].host_id);
    }
  }, [hostId, onlineHosts]);

  // The server persists each session as its frame arrives, so refresh the
  // sidebar list every 5s while the import is in flight — sessions show up as
  // they land instead of all at once when the request finally returns.
  useEffect(() => {
    if (!submitting) return;
    const id = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    }, 5000);
    return () => clearInterval(id);
  }, [submitting, queryClient]);

  async function handleImport(): Promise<void> {
    if (hostId === null) return;
    const exactSessionId = sessionId.trim();
    if (mode === "session" && exactSessionId.length === 0) return;
    setSubmitting(true);
    setError(null);
    setResult(null);
    setStreamed([]);
    setProgress(null);
    let importError: ImportErrorInfo | null = null;
    let thrown: unknown = null;
    try {
      const onSession = (s: ImportedSessionRef) => setStreamed((prev) => [...prev, s]);
      const res = await importLocalSessions(
        hostId,
        source,
        limit,
        onSession,
        mode === "session" ? exactSessionId : undefined,
        { onProgress: setProgress },
      );
      setResult(res);
      importError = res.error;
      // Newly imported sessions land in the sidebar list.
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
    } catch (e) {
      thrown = e;
      const host = (hosts ?? []).find((h) => h.host_id === hostId);
      importError = importErrorFromException(e, { hostName: host?.name ?? null });
    } finally {
      setError(importError);
      setSubmitting(false);
    }
    if (importError !== null && hostListLooksStale(importError, thrown)) {
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
    }
  }

  // "Retry failed (N)" counts only failures a re-run can fix; a retryable
  // whole-import error (dropped connection, time limit) offers a plain re-run.
  // Nothing retryable → no button: the messages say what to change first.
  const retryableFailures = result?.failures.filter((f) => f.retryable).length ?? 0;
  const retryLabel =
    retryableFailures > 0
      ? `Retry failed (${retryableFailures})`
      : error?.retryable
        ? "Import again"
        : null;
  const summary = result !== null ? importSummary(result) : null;
  const noHostsOnline = onlineHosts.length === 0;
  const hasOutcome = result !== null || streamed.length > 0 || submitting || error !== null;

  const noHostsNotice = (
    <p className="text-sm text-muted-foreground" data-testid="import-no-hosts">
      None of your machines are online. Start one with{" "}
      <code className="rounded bg-muted px-1 py-0.5 font-mono">omnigent host</code> from your
      terminal, then return here.
    </p>
  );

  const outcome = hasOutcome && (
    <div
      className="mt-4 flex flex-col gap-2 border-t border-border pt-4"
      data-testid="import-outcome"
    >
      {submitting ? (
        <p className="text-sm text-muted-foreground" data-testid="import-progress">
          {progressText(progress, streamed.length)}
        </p>
      ) : summary !== null ? (
        <p className="text-sm text-muted-foreground" data-testid="import-result">
          {summary}
        </p>
      ) : null}
      {error !== null && !submitting && <ImportErrorBanner error={error} />}
      {streamed.length > 0 && (
        <ul
          className="flex max-h-64 flex-col gap-1 overflow-y-auto"
          data-testid="import-result-sessions"
        >
          {streamed.map((s) => (
            <li key={s.id}>
              <Link
                to={`/c/${s.id}`}
                className="block truncate text-sm text-primary hover:underline"
                data-testid={`import-result-link-${s.id}`}
              >
                {s.title || "Untitled session"}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {result !== null && result.failures.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="import-failures">
          <span className="text-sm font-medium text-destructive">
            {result.failed} couldn't be imported
          </span>
          <ul className="flex max-h-48 flex-col gap-2 overflow-y-auto">
            {result.failures.map((f, i) => (
              <ImportFailureRow key={f.externalSessionId ?? `failure-${i}`} failure={f} />
            ))}
          </ul>
        </div>
      )}
      {/* A re-run needs an online machine; without one the notice says what to do. */}
      {retryLabel !== null && !submitting && !noHostsOnline && (
        <div>
          <Button
            variant="outline"
            size="sm"
            data-testid="import-retry"
            onClick={() => void handleImport()}
          >
            {retryLabel}
          </Button>
          <p className="mt-1 text-xs text-muted-foreground">
            Retrying re-runs the import; sessions already imported are skipped.
          </p>
        </div>
      )}
    </div>
  );

  if (noHostsOnline) {
    // The machine can go offline mid-import (that is often why it stopped);
    // keep what was imported and why it stopped next to the offline notice.
    if (!hasOutcome) return noHostsNotice;
    return (
      <div className="flex flex-col" data-testid="import-sessions-panel">
        {noHostsNotice}
        {outcome}
      </div>
    );
  }

  return (
    <div className="flex flex-col" data-testid="import-sessions-panel">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border pb-4">
        <span className="text-ui font-medium">Machine</span>
        <Select value={hostId ?? ""} onValueChange={(v) => setHostId(v)}>
          <SelectTrigger className="w-full sm:w-72 sm:shrink-0" data-testid="import-host-select">
            <SelectValue placeholder="Select a machine" />
          </SelectTrigger>
          <SelectContent>
            {onlineHosts.map((host) => (
              <SelectItem
                key={host.host_id}
                value={host.host_id}
                data-testid={`import-host-${host.host_id}`}
              >
                <HostLabel host={host} />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border py-4">
        <span className="text-ui font-medium">Import</span>
        <Select
          value={mode}
          onValueChange={(value) => {
            const nextMode = value as ImportMode;
            setMode(nextMode);
            if (nextMode === "session" && source === "all") setSource("claude");
          }}
        >
          <SelectTrigger className="w-full sm:w-56 sm:shrink-0" data-testid="import-mode-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="recent">Recent sessions</SelectItem>
            <SelectItem value="session">Session by ID</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border py-4">
        <span className="text-ui font-medium">Harness</span>
        <Select value={source} onValueChange={(v) => setSource(v as ImportSourceSelector)}>
          <SelectTrigger className="w-full sm:w-56 sm:shrink-0" data-testid="import-source-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SOURCES.filter((s) => mode === "recent" || s.value !== "all").map((s) => (
              <SelectItem key={s.value} value={s.value} data-testid={`import-source-${s.value}`}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {mode === "recent" ? (
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-border py-4">
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="text-ui font-medium">
              How many recent sessions{source === "all" ? " (across all harnesses)" : ""}
            </span>
            <p className="text-sm text-muted-foreground" data-testid="import-limit-help">
              The most recent sessions you opened in the harness. Sub-agent and automation runs are
              skipped, and sessions you've already imported are counted separately, not re-imported.
            </p>
          </div>
          <Select value={String(limit)} onValueChange={(v) => setLimit(Number(v))}>
            <SelectTrigger className="w-full sm:w-56 sm:shrink-0" data-testid="import-limit-select">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LIMITS.map((n) => (
                <SelectItem key={n} value={String(n)}>
                  Last {n}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border py-4">
          <label htmlFor="import-session-id" className="text-ui font-medium">
            Session ID
          </label>
          <Input
            id="import-session-id"
            data-testid="import-session-id"
            value={sessionId}
            maxLength={128}
            autoComplete="off"
            placeholder="Enter a session ID"
            className="w-full sm:w-56 sm:shrink-0"
            onChange={(event) => setSessionId(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void handleImport();
            }}
          />
        </div>
      )}

      <div className="flex justify-end pt-4">
        <Button
          data-testid="import-submit"
          loading={submitting}
          disabled={hostId === null || (mode === "session" && sessionId.trim().length === 0)}
          onClick={() => void handleImport()}
        >
          Import
        </Button>
      </div>

      {outcome}
    </div>
  );
}
