import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, ChevronRightIcon } from "lucide-react";
import { useHosts } from "@/hooks/useHosts";
import { Link } from "@/lib/routing";
import { HostLabel } from "./HostLabel";
import { CliCommandBlock, renderTextWithInlineCode } from "./CliCommandBlock";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SettingsLabel } from "@/components/SettingsLabel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type {
  ImportErrorInfo,
  ImportFailureRef,
  ImportProgress,
  ImportSourceSelector,
  LocalImportResult,
} from "@/lib/sessionsApi";
import { useSessionImportStore } from "@/store/sessionImport";

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

const LIMITS = [25, 50, 100, 200, 500, 1000];
type ImportMode = "recent" | "session";

/**
 * "Imported 12 · 2 failed" (zero parts omitted), or null when there is nothing
 * to report beside an error. Already-imported and skipped (empty) sessions get
 * their own lines (`alreadyImportedText`, `skippedText`), so they aren't
 * counted here. A run that stopped early (`complete: false`) never reads
 * "Nothing new to import": it leads with what it imported and says it stopped,
 * e.g. "Imported 0 · stopped early".
 */
function importSummary(result: LocalImportResult): string | null {
  const stoppedEarly = !result.complete || result.error !== null;
  const parts = [
    result.imported > 0 ? `Imported ${result.imported}` : null,
    result.failed > 0 ? `${result.failed} failed` : null,
  ].filter((p): p is string => p !== null);
  if (stoppedEarly) {
    if (parts.length === 0 && result.alreadyImported === 0 && result.skipped === 0) return null;
    if (result.imported === 0) parts.unshift("Imported 0");
    return [...parts, "stopped early"].join(" · ");
  }
  if (result.imported === 0 && result.failed === 0) {
    if (result.alreadyImported > 0 || result.skipped > 0) return "Nothing new to import";
    return result.error === null ? "No sessions to import." : null;
  }
  return parts.join(" · ");
}

/** "1 session was already imported before" / "45 sessions were …"; null for none. */
function alreadyImportedText(count: number): string | null {
  if (count <= 0) return null;
  return count === 1
    ? "1 session was already imported before"
    : `${count} sessions were already imported before`;
}

/** "1 session skipped: no history to import" / "3 sessions skipped: …". */
function skippedText(count: number): string {
  return `${count} ${count === 1 ? "session" : "sessions"} skipped: no history to import`;
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

/**
 * Sessions skipped because they hold no history (opened and closed without a
 * prompt). A warning, not an error: nothing was lost, so they are not counted
 * as failed and offer no retry. Collapsed so a long list stays one line.
 */
function ImportSkippedNotice({ count, sessions }: { count: number; sessions: ImportFailureRef[] }) {
  return (
    <details className="group text-sm" data-testid="import-skipped">
      <summary
        className="flex cursor-pointer list-none items-start gap-1.5 text-warning select-none [&::-webkit-details-marker]:hidden"
        data-testid="import-skipped-summary"
      >
        <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <span>{skippedText(count)}</span>
        <ChevronRightIcon
          className="mt-0.5 size-3.5 shrink-0 transition-transform group-open:rotate-90"
          aria-hidden="true"
        />
      </summary>
      <div className="mt-1 flex flex-col gap-1 pl-5">
        <p className="text-xs text-muted-foreground">
          These sessions have no messages, so there is nothing to import.
        </p>
        {sessions.length > 0 && (
          <ul className="flex max-h-48 flex-col gap-0.5 overflow-y-auto">
            {sessions.map((s, i) => {
              const label = [sourceLabel(s.source), s.externalSessionId].filter(
                (p): p is string => p !== null,
              );
              return (
                <li
                  key={s.externalSessionId ?? `skipped-${i}`}
                  className="font-mono text-xs text-muted-foreground"
                  data-testid="import-skipped-item"
                >
                  {label.length > 0 ? label.join(" · ") : s.reason}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </details>
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
 * server-side; the result links each newly imported session. The run lives in
 * a store, so leaving Settings keeps it going and the sidebar shows its
 * progress; returning picks the same run back up.
 */
export function ImportSessionsPanel() {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const { importing, importedCount, streamed, progress, request, result, error, start } =
    useSessionImportStore();
  const [hostId, setHostId] = useState<string | null>(request?.hostId ?? null);
  const [source, setSource] = useState<ImportSourceSelector>(request?.source ?? "all");
  const [limit, setLimit] = useState(request?.limit ?? 25);
  const [mode, setMode] = useState<ImportMode>(request?.sessionId ? "session" : "recent");
  const [sessionId, setSessionId] = useState(request?.sessionId ?? "");

  // Default to the caller's current online machine — the transcripts are read
  // on the host, so there's nothing to import without one.
  useEffect(() => {
    if (hostId === null && onlineHosts.length > 0) {
      setHostId(onlineHosts[0].host_id);
    }
  }, [hostId, onlineHosts]);

  async function handleImport(): Promise<void> {
    if (hostId === null) return;
    const exactSessionId = sessionId.trim();
    if (mode === "session" && exactSessionId.length === 0) return;
    const hostName = (hosts ?? []).find((h) => h.host_id === hostId)?.name ?? null;
    await start(
      mode === "session"
        ? { hostId, source, limit, sessionId: exactSessionId, hostName }
        : { hostId, source, limit, hostName },
      queryClient,
    );
  }

  function handleRetry(): void {
    if (!request) return;
    setHostId(request.hostId);
    setSource(request.source);
    setLimit(request.limit);
    setMode(request.sessionId ? "session" : "recent");
    setSessionId(request.sessionId ?? "");
    void start(request, queryClient);
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
  const alreadyImported = result !== null ? alreadyImportedText(result.alreadyImported) : null;
  const noHostsOnline = onlineHosts.length === 0;
  const hasOutcome = result !== null || streamed.length > 0 || importing || error !== null;

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
      {importing ? (
        <p className="text-sm text-muted-foreground" data-testid="import-progress">
          {progressText(progress, importedCount)}
        </p>
      ) : summary !== null ? (
        <p className="text-sm text-muted-foreground" data-testid="import-result">
          {summary}
        </p>
      ) : null}
      {alreadyImported !== null && !importing && (
        <p className="text-sm text-muted-foreground" data-testid="import-already-imported">
          {alreadyImported}
        </p>
      )}
      {result !== null && result.skipped > 0 && !importing && (
        <ImportSkippedNotice count={result.skipped} sessions={result.skippedSessions} />
      )}
      {error !== null && !importing && <ImportErrorBanner error={error} />}
      {streamed.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="import-new-sessions">
          <span className="text-sm font-medium" data-testid="import-new-sessions-heading">
            Newly imported sessions
          </span>
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
        </div>
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
      {retryLabel !== null && !importing && !noHostsOnline && (
        <div>
          <Button variant="outline" size="sm" data-testid="import-retry" onClick={handleRetry}>
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
        <span className="text-ui font-normal md:font-medium">Machine</span>
        <Select value={hostId ?? ""} onValueChange={(v) => setHostId(v)} disabled={importing}>
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
        <span className="text-ui font-normal md:font-medium">Import</span>
        <Select
          value={mode}
          disabled={importing}
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
        <span className="text-ui font-normal md:font-medium">Harness</span>
        <Select
          value={source}
          onValueChange={(v) => setSource(v as ImportSourceSelector)}
          disabled={importing}
        >
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
          <SettingsLabel
            label={`How many recent sessions${source === "all" ? " (across all harnesses)" : ""}`}
            className="flex-1"
            description="The most recent sessions you opened in the harness. Sub-agent and automation runs are skipped, and sessions you've already imported are counted separately, not re-imported."
            descriptionTestId="import-limit-help"
          />
          <Select
            value={String(limit)}
            onValueChange={(v) => setLimit(Number(v))}
            disabled={importing}
          >
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
          <label htmlFor="import-session-id" className="text-ui font-normal md:font-medium">
            Session ID
          </label>
          <Input
            id="import-session-id"
            data-testid="import-session-id"
            value={sessionId}
            disabled={importing}
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
          loading={importing}
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
