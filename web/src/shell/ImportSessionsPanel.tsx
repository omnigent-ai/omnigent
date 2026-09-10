import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useHosts } from "@/hooks/useHosts";
import { Link } from "@/lib/routing";
import { HostLabel } from "./HostLabel";
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
  importLocalSessions,
  type ImportedSessionRef,
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
  const [result, setResult] = useState<LocalImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    try {
      const onSession = (s: ImportedSessionRef) => setStreamed((prev) => [...prev, s]);
      const res =
        mode === "session"
          ? await importLocalSessions(hostId, source, limit, onSession, exactSessionId)
          : await importLocalSessions(hostId, source, limit, onSession);
      setResult(res);
      // Newly imported sessions land in the sidebar list.
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Import failed. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (onlineHosts.length === 0) {
    return (
      <p className="max-w-md text-sm text-muted-foreground" data-testid="import-no-hosts">
        None of your machines are online. Start one with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">omnigent host</code> from your
        terminal, then return here.
      </p>
    );
  }

  return (
    <div className="flex max-w-md flex-col gap-4" data-testid="import-sessions-panel">
      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium text-muted-foreground">Machine</span>
        <Select value={hostId ?? ""} onValueChange={(v) => setHostId(v)}>
          <SelectTrigger className="w-full text-sm" data-testid="import-host-select">
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

      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium text-muted-foreground">Import</span>
        <Select
          value={mode}
          onValueChange={(value) => {
            const nextMode = value as ImportMode;
            setMode(nextMode);
            if (nextMode === "session" && source === "all") setSource("claude");
          }}
        >
          <SelectTrigger className="w-full text-sm" data-testid="import-mode-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="recent">Recent sessions</SelectItem>
            <SelectItem value="session">Session by ID</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium text-muted-foreground">Harness</span>
        <Select value={source} onValueChange={(v) => setSource(v as ImportSourceSelector)}>
          <SelectTrigger className="w-full text-sm" data-testid="import-source-select">
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
        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium text-muted-foreground">
            How many recent sessions{source === "all" ? " (across all harnesses)" : ""}
          </span>
          <Select value={String(limit)} onValueChange={(v) => setLimit(Number(v))}>
            <SelectTrigger className="w-full text-sm" data-testid="import-limit-select">
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
        <div className="flex flex-col gap-2">
          <label htmlFor="import-session-id" className="text-sm font-medium text-muted-foreground">
            Session ID
          </label>
          <Input
            id="import-session-id"
            data-testid="import-session-id"
            value={sessionId}
            maxLength={128}
            autoComplete="off"
            placeholder="Enter a session ID"
            onChange={(event) => setSessionId(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void handleImport();
            }}
          />
        </div>
      )}

      <div>
        <Button
          data-testid="import-submit"
          loading={submitting}
          disabled={hostId === null || (mode === "session" && sessionId.trim().length === 0)}
          onClick={() => void handleImport()}
        >
          Import
        </Button>
      </div>

      {(result !== null || streamed.length > 0 || submitting) && (
        <div className="flex flex-col gap-2">
          {result !== null ? (
            <p className="text-sm text-muted-foreground" data-testid="import-result">
              Imported {result.imported}
              {result.alreadyImported > 0 && `, ${result.alreadyImported} already imported`}
              {result.failed > 0 && `, ${result.failed} failed`}.
            </p>
          ) : submitting ? (
            <p className="text-sm text-muted-foreground" data-testid="import-progress">
              Importing{streamed.length > 0 ? ` ${streamed.length} so far` : ""}…
            </p>
          ) : null}
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
        </div>
      )}
      {error !== null && (
        <p className="text-sm text-destructive" data-testid="import-error">
          {error}
        </p>
      )}
    </div>
  );
}
