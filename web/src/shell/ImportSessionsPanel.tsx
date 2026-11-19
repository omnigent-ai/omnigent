import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useHosts } from "@/hooks/useHosts";
import { Link } from "@/lib/routing";
import { HostLabel } from "./HostLabel";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Input } from "@/components/ui/input";
import { SettingsLabel } from "@/components/SettingsLabel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ImportSourceSelector } from "@/lib/sessionsApi";
import { useSessionImportStore } from "@/store/sessionImport";
import {
  IMPORT_HARNESS_ENTRY,
  IMPORT_MACHINE_ENTRY,
  IMPORT_MODE_ENTRY,
  IMPORT_RECENT_COUNT_ENTRY,
  IMPORT_RECENT_LIMITS,
  IMPORT_RUN_ENTRY,
  IMPORT_SESSION_ID_ENTRY,
  IMPORT_SOURCES,
  importRecentLimitLabel,
} from "@/pages/settings/search/catalog/import";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import {
  settingsControlProps,
  settingsDescriptionId,
  settingsLabelId,
} from "@/pages/settings/search/settingsControl";
import {
  settingsOverlayProps,
  settingsRuntimeChoiceProps,
} from "@/pages/settings/search/settingsOwnership";

type ImportMode = "recent" | "session";

/** The shared classes of a row's line: its label and its control, wrapped and spread apart. */
const ROW_CLASS = "flex flex-wrap items-center justify-between gap-x-6 gap-y-3";

/**
 * Inline (non-modal) import UI for the Settings "Import sessions" section.
 * Imports recent transcripts or one known harness session through the chosen
 * host, then refreshes the sidebar. Already-imported sessions are skipped
 * server-side; progress and results show aggregate counts.
 */
export function ImportSessionsPanel() {
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  // The import run lives in a store so it survives leaving Settings; the
  // sidebar shows the same in-flight state.
  const { importing, importedCount, request, result, error, start } = useSessionImportStore();
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
    await start(
      mode === "session"
        ? { hostId, source, limit, sessionId: exactSessionId }
        : { hostId, source, limit },
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

  if (onlineHosts.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="import-no-hosts">
        None of your machines are online. Start one with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">omnigent host</code> from your
        terminal, then return here.
      </p>
    );
  }

  return (
    <div className="flex flex-col" data-testid="import-sessions-panel">
      <div className="border-b border-border pb-4">
        <SettingsItem entry={IMPORT_MACHINE_ENTRY} className={ROW_CLASS}>
          <span className="text-ui font-normal md:font-medium">
            {IMPORT_MACHINE_ENTRY.parts?.machine}
          </span>
          <Select value={hostId ?? ""} onValueChange={(v) => setHostId(v)} disabled={importing}>
            <SelectTrigger
              {...settingsControlProps(IMPORT_MACHINE_ENTRY, "machine")}
              className="w-full sm:w-72 sm:shrink-0"
              data-testid="import-host-select"
            >
              <SelectValue placeholder="Select a machine" />
            </SelectTrigger>
            <SelectContent {...settingsOverlayProps(IMPORT_MACHINE_ENTRY, "machine")}>
              {onlineHosts.map((host) => (
                <SelectItem
                  {...settingsRuntimeChoiceProps(IMPORT_MACHINE_ENTRY, "machine")}
                  key={host.host_id}
                  value={host.host_id}
                  data-testid={`import-host-${host.host_id}`}
                >
                  <HostLabel host={host} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </SettingsItem>
      </div>

      <div className="border-b border-border py-4">
        <SettingsItem entry={IMPORT_MODE_ENTRY} className={ROW_CLASS}>
          <span className="text-ui font-normal md:font-medium">
            {IMPORT_MODE_ENTRY.parts?.mode}
          </span>
          <Select
            value={mode}
            disabled={importing}
            onValueChange={(value) => {
              const nextMode = value as ImportMode;
              setMode(nextMode);
              if (nextMode === "session" && source === "all") setSource("claude");
            }}
          >
            <SelectTrigger
              {...settingsControlProps(IMPORT_MODE_ENTRY, "mode")}
              className="w-full sm:w-56 sm:shrink-0"
              data-testid="import-mode-select"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent {...settingsOverlayProps(IMPORT_MODE_ENTRY, "mode")}>
              <SelectItem value="recent">Recent sessions</SelectItem>
              <SelectItem value="session">Session by ID</SelectItem>
            </SelectContent>
          </Select>
        </SettingsItem>
      </div>

      <div className="border-b border-border py-4">
        <SettingsItem entry={IMPORT_HARNESS_ENTRY} className={ROW_CLASS}>
          <span className="text-ui font-normal md:font-medium">Harness</span>
          <Select
            value={source}
            onValueChange={(v) => setSource(v as ImportSourceSelector)}
            disabled={importing}
          >
            <SelectTrigger
              {...settingsControlProps(IMPORT_HARNESS_ENTRY, "harness")}
              className="w-full sm:w-56 sm:shrink-0"
              data-testid="import-source-select"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent {...settingsOverlayProps(IMPORT_HARNESS_ENTRY, "harness")}>
              {IMPORT_SOURCES.filter((s) => mode === "recent" || s.value !== "all").map((s) => (
                <SelectItem key={s.value} value={s.value} data-testid={`import-source-${s.value}`}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </SettingsItem>
      </div>

      {mode === "recent" ? (
        <div className="border-b border-border py-4">
          <SettingsItem
            entry={IMPORT_RECENT_COUNT_ENTRY}
            className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3"
          >
            <SettingsLabel
              label={`${IMPORT_RECENT_COUNT_ENTRY.label}${source === "all" ? " (across all harnesses)" : ""}`}
              labelId={settingsLabelId(IMPORT_RECENT_COUNT_ENTRY)}
              descriptionId={settingsDescriptionId(IMPORT_RECENT_COUNT_ENTRY)}
              className="flex-1"
              description={IMPORT_RECENT_COUNT_ENTRY.description}
              descriptionTestId="import-limit-help"
            />
            <Select
              value={String(limit)}
              onValueChange={(v) => setLimit(Number(v))}
              disabled={importing}
            >
              <SelectTrigger
                {...settingsControlProps(IMPORT_RECENT_COUNT_ENTRY)}
                className="w-full sm:w-56 sm:shrink-0"
                data-testid="import-limit-select"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent {...settingsOverlayProps(IMPORT_RECENT_COUNT_ENTRY)}>
                {IMPORT_RECENT_LIMITS.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {importRecentLimitLabel(n)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </SettingsItem>
        </div>
      ) : (
        <div className="border-b border-border py-4">
          <SettingsItem entry={IMPORT_SESSION_ID_ENTRY} className={ROW_CLASS}>
            <label htmlFor="import-session-id" className="text-ui font-normal md:font-medium">
              Session ID
            </label>
            <Input
              {...settingsControlProps(IMPORT_SESSION_ID_ENTRY, "sessionId")}
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
          </SettingsItem>
        </div>
      )}

      <SettingsItem entry={IMPORT_RUN_ENTRY} className="flex flex-col">
        <div className="flex justify-end pt-4">
          <Button
            {...settingsControlProps(IMPORT_RUN_ENTRY, "import")}
            data-testid="import-submit"
            loading={importing}
            disabled={hostId === null || (mode === "session" && sessionId.trim().length === 0)}
            onClick={() => void handleImport()}
          >
            Import
          </Button>
        </div>

        {(result !== null || importedCount > 0 || importing) && (
          <div className="mt-4 flex flex-col gap-2 border-t border-border pt-4">
            {result !== null ? (
              <p className="text-sm text-muted-foreground" data-testid="import-result">
                Imported {result.imported}
                {result.alreadyImported > 0 && `, ${result.alreadyImported} already imported`}
                {result.failed > 0 && `, ${result.failed} failed`}.
              </p>
            ) : importing ? (
              <p
                role="status"
                className="flex items-center gap-2 text-sm"
                data-testid="import-progress"
              >
                <Spinner className="size-4 shrink-0" />
                Importing sessions… {importedCount} imported
              </p>
            ) : importedCount > 0 ? (
              <p className="text-sm text-muted-foreground">
                Imported {importedCount} before the import stopped.
              </p>
            ) : null}
            {importedCount > 0 && (
              <Link
                {...settingsControlProps(IMPORT_RUN_ENTRY, "viewSessions")}
                to="/"
                className="w-fit text-sm text-primary hover:underline"
              >
                View sessions
              </Link>
            )}
            {result !== null && result.failures.length > 0 && (
              <div className="flex flex-col gap-2" data-testid="import-failures">
                <details>
                  <summary
                    {...settingsControlProps(IMPORT_RUN_ENTRY, "failures", {
                      runtime: String(result.failed),
                    })}
                    className="cursor-pointer text-sm text-destructive"
                  >
                    View {result.failed} failed sessions
                  </summary>
                  <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto">
                    {result.failures.map((f, i) => (
                      <li
                        key={f.externalSessionId ?? `failure-${i}`}
                        className="text-sm text-muted-foreground"
                        data-testid="import-failure-item"
                      >
                        <span className="text-destructive">{f.reason}</span>
                        {f.externalSessionId !== null && (
                          <span className="ml-1 font-mono text-xs">({f.externalSessionId})</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </details>
                <div>
                  <Button
                    {...settingsControlProps(IMPORT_RUN_ENTRY, "retry")}
                    variant="outline"
                    size="sm"
                    data-testid="import-retry"
                    loading={importing}
                    onClick={handleRetry}
                  >
                    Retry failed
                  </Button>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Retrying re-runs the import; sessions already imported are skipped.
                  </p>
                </div>
              </div>
            )}
          </div>
        )}
      </SettingsItem>
      {error !== null && (
        <p className="text-sm text-destructive" data-testid="import-error">
          {error}
        </p>
      )}
    </div>
  );
}
