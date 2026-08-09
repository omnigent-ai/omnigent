import { useEffect, useMemo, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useHosts } from "@/hooks/useHosts";
import { useHostLocalSessions } from "@/hooks/useHostLocalSessions";
import {
  importHostLocalSession,
  type ImportSourceId,
  type LocalSessionSummary,
} from "@/lib/localSessionImportApi";

/** Human labels for the two importable chat sources, used in copy and tabs. */
const SOURCE_LABELS: Record<ImportSourceId, string> = {
  claude: "Claude Code",
  codex: "Codex",
};

const SOURCES: readonly ImportSourceId[] = ["claude", "codex"];

export interface ImportChatDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Host preselected from the composer's host chip, if any. */
  defaultHostId?: string | null;
  /** Called with the created Omnigent session id after a successful import. */
  onImported: (sessionId: string) => void;
}

/**
 * "Import chat" dialog — browse the Claude Code / Codex chats that already
 * exist on a connected machine and pull one into Omnigent as a new session.
 *
 * A thin `Dialog` shell around {@link ImportChatForm}, which holds all the
 * state. The form lives inside `DialogContent`, so closing the dialog unmounts
 * it and a reopen starts clean — no stale pick or error from last time.
 *
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 * @param defaultHostId - Host preselected from the composer's host chip.
 * @param onImported - Receives the created session id after a successful
 *   import, just before the dialog closes.
 */
export function ImportChatDialog({
  open,
  onOpenChange,
  defaultHostId,
  onImported,
}: ImportChatDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="import-chat-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Import chat</DialogTitle>
          <DialogDescription>
            Bring a Claude Code or Codex chat that already exists on a connected machine into
            Omnigent as a new session.
          </DialogDescription>
        </DialogHeader>
        <ImportChatForm
          defaultHostId={defaultHostId}
          onImported={onImported}
          onClose={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}

/**
 * The import picker itself — source toggle, machine picker, session-ID field,
 * and the browsable list of the machine's recent chats.
 *
 * Clicking a chat fills the session-ID field and narrows the list to that one
 * row; clearing the selection brings the whole list back. The field stays
 * editable throughout, so a session id can always be pasted by hand — the
 * manual path the CLI already supports. A failed import surfaces the server's
 * own message inline and leaves everything editable for a straight retry.
 *
 * @param defaultHostId - Host to preselect when it is online.
 * @param onImported - Called with the new session id on success.
 * @param onClose - Closes the host dialog (Cancel, and after a successful
 *   import).
 */
function ImportChatForm({
  defaultHostId,
  onImported,
  onClose,
}: {
  defaultHostId?: string | null;
  onImported: (sessionId: string) => void;
  onClose: () => void;
}) {
  const [source, setSource] = useState<ImportSourceId>("claude");
  const [hostId, setHostId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [selected, setSelected] = useState<LocalSessionSummary | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const { data: hosts } = useHosts();
  // Only a live, user-connected machine has transcripts to import: sandbox
  // hosts are server-managed launch targets. A host from an older server omits
  // `sandbox_provider` entirely, which must read as "not a sandbox".
  const onlineHosts = useMemo(
    () => (hosts ?? []).filter((h) => h.status === "online" && !h.sandbox_provider),
    [hosts],
  );
  const noOnlineHosts = onlineHosts.length === 0;

  // Default to the composer's host when it's online, else the first online
  // one. Only fills an empty slot, so an explicit pick is never overridden.
  useEffect(() => {
    if (hostId !== null || onlineHosts.length === 0) return;
    const preferred = onlineHosts.find((h) => h.host_id === defaultHostId);
    setHostId(preferred?.host_id ?? onlineHosts[0].host_id);
  }, [hostId, defaultHostId, onlineHosts]);

  // The form only mounts while the dialog is open, so the browse is always on.
  const recent = useHostLocalSessions(hostId, source, true);
  const rows = recent.data ?? [];
  // An import failure takes precedence: it's the action the user just took.
  const shownError = errorMessage ?? recent.error?.message ?? null;

  function clearSelection(): void {
    setSelected(null);
    setSessionId("");
    setExpandedId(null);
    setErrorMessage(null);
  }

  function selectRow(row: LocalSessionSummary): void {
    setSelected(row);
    setSessionId(row.external_session_id);
    setErrorMessage(null);
  }

  function changeSource(next: ImportSourceId): void {
    if (next === source) return;
    setSource(next);
    // A pick from the other source isn't valid here, and its id would import
    // the wrong transcript.
    clearSelection();
  }

  async function handleImport(): Promise<void> {
    const externalId = sessionId.trim();
    if (hostId === null || externalId === "" || importing) return;
    setImporting(true);
    setErrorMessage(null);
    try {
      const result = await importHostLocalSession(hostId, source, externalId);
      onImported(result.session_id);
      onClose();
    } catch (e) {
      // The server's text is the useful part ("host is offline", "already
      // imported as conv_1"), so show it verbatim and stay open for a retry.
      setErrorMessage(e instanceof Error ? e.message : "Couldn't import that chat. Try again.");
    } finally {
      setImporting(false);
    }
  }

  const canImport = hostId !== null && sessionId.trim() !== "" && !importing;
  // Picking a chat narrows the list to that one row; clearing restores it.
  const visibleRows = selected !== null ? [selected] : rows;

  return (
    <>
      <div className="-mr-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-4 [scrollbar-width:thin] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
        <div className="flex flex-col gap-1.5">
          <span id="import-chat-source-label" className="text-sm font-medium text-muted-foreground">
            Chat source
          </span>
          <div
            role="group"
            aria-labelledby="import-chat-source-label"
            className="inline-flex self-start gap-1 rounded-lg bg-muted p-[3px]"
          >
            {SOURCES.map((id) => (
              <Button
                key={id}
                type="button"
                size="sm"
                variant={source === id ? "secondary" : "ghost"}
                aria-pressed={source === id}
                disabled={noOnlineHosts}
                data-testid={`import-chat-source-${id}`}
                onClick={() => changeSource(id)}
              >
                {SOURCE_LABELS[id]}
              </Button>
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-muted-foreground">Machine</span>
          <Select
            value={hostId ?? ""}
            onValueChange={(v) => {
              setHostId(v);
              // Chats are per-machine, so a pick from the old host is stale.
              clearSelection();
            }}
            disabled={noOnlineHosts}
          >
            <SelectTrigger
              aria-label="Machine"
              data-testid="import-chat-host-select"
              className="w-full text-sm"
            >
              <SelectValue placeholder="Select a machine" />
            </SelectTrigger>
            <SelectContent>
              {onlineHosts.map((h) => (
                <SelectItem
                  key={h.host_id}
                  value={h.host_id}
                  data-testid={`import-chat-host-option-${h.host_id}`}
                >
                  <span className="font-mono text-sm">{h.name}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {noOnlineHosts && (
            <p className="text-sm text-muted-foreground">Connect a machine to import chats</p>
          )}
        </div>

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="import-chat-session-id"
            className="text-sm font-medium text-muted-foreground"
          >
            Session ID
          </label>
          <Input
            id="import-chat-session-id"
            data-testid="import-chat-session-id"
            value={sessionId}
            onChange={(e) => {
              setSessionId(e.target.value);
              // A hand-edited id no longer describes the picked row, so drop the
              // selection rather than show a chat that isn't the import target.
              setSelected(null);
            }}
            placeholder="Pick a chat below, or paste a session ID"
            className="font-mono"
          />
        </div>

        {/* A height floor keeps the dialog from collapsing while a source
            switch refetches — the list is the only part that resizes. */}
        <div className="flex min-h-36 flex-col gap-2">
          <span id="import-chat-recent-label" className="text-sm font-medium text-muted-foreground">
            {selected !== null ? "Selected chat" : `Recent ${SOURCE_LABELS[source]} chats`}
          </span>
          {hostId === null ? null : recent.isLoading ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner className="size-3.5" />
              Loading recent chats…
            </p>
          ) : recent.error ? (
            // The server's wording renders once, in the shared error line below.
            <p className="text-sm text-muted-foreground">Couldn't list this machine's chats.</p>
          ) : visibleRows.length === 0 ? (
            <p data-testid="import-chat-empty" className="text-sm text-muted-foreground">
              No recent {SOURCE_LABELS[source]} chats on this machine
            </p>
          ) : (
            <ul
              data-testid="import-chat-recent-list"
              aria-labelledby="import-chat-recent-label"
              className="flex flex-col gap-1.5"
            >
              {visibleRows.map((row) => (
                <RecentRow
                  key={row.external_session_id}
                  row={row}
                  selected={selected?.external_session_id === row.external_session_id}
                  expanded={expandedId === row.external_session_id}
                  onSelect={() => selectRow(row)}
                  onToggleExpand={() =>
                    setExpandedId((id) =>
                      id === row.external_session_id ? null : row.external_session_id,
                    )
                  }
                />
              ))}
            </ul>
          )}
          {selected !== null && (
            <Button
              variant="ghost"
              size="sm"
              className="self-start"
              data-testid="import-chat-clear-selection"
              onClick={clearSelection}
            >
              Clear selection
            </Button>
          )}
        </div>
      </div>

      {shownError !== null && (
        <p data-testid="import-chat-error" className="text-sm text-destructive">
          {shownError}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={importing}>
          Cancel
        </Button>
        <Button data-testid="import-chat-submit" onClick={handleImport} disabled={!canImport}>
          {importing && <Spinner className="size-3.5" />}
          {importing ? "Importing…" : "Import"}
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * One browsable chat: a click target that selects it, plus a disclosure
 * button revealing the preview turns the host sent along.
 */
function RecentRow({
  row,
  selected,
  expanded,
  onSelect,
  onToggleExpand,
}: {
  row: LocalSessionSummary;
  selected: boolean;
  expanded: boolean;
  onSelect: () => void;
  onToggleExpand: () => void;
}) {
  // The host can't always recover a title or a workspace from the transcript.
  const title = row.title ?? "Untitled session";
  const previewId = `import-chat-preview-${row.external_session_id}`;
  return (
    <li className="rounded-lg border border-border">
      <div className="flex items-start">
        <button
          type="button"
          data-testid={`import-chat-recent-row-${row.external_session_id}`}
          aria-pressed={selected}
          onClick={onSelect}
          className="flex min-w-0 flex-1 cursor-pointer flex-col items-start gap-0.5 rounded-l-lg px-3 py-2 text-left transition-colors hover:bg-muted"
        >
          <span className="w-full truncate text-sm font-medium">{title}</span>
          <span className="flex w-full min-w-0 gap-2 text-xs text-muted-foreground">
            <span className="truncate font-mono">{row.workspace ?? "Unknown folder"}</span>
            <span className="shrink-0">
              {row.item_count} {row.item_count === 1 ? "item" : "items"}
            </span>
          </span>
        </button>
        <button
          type="button"
          data-testid={`import-chat-expand-${row.external_session_id}`}
          aria-expanded={expanded}
          aria-controls={expanded ? previewId : undefined}
          aria-label={`${expanded ? "Hide" : "Show"} preview of ${title}`}
          onClick={onToggleExpand}
          className="cursor-pointer rounded-r-lg px-2 py-3 text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? (
            <ChevronDownIcon className="size-4" />
          ) : (
            <ChevronRightIcon className="size-4" />
          )}
        </button>
      </div>
      {expanded && (
        <div
          id={previewId}
          data-testid={previewId}
          className="flex flex-col gap-1 border-t border-border px-3 py-2 text-xs"
        >
          {row.preview.length === 0 ? (
            <p className="text-muted-foreground">No preview available</p>
          ) : (
            row.preview.map((message, i) => (
              // Preview turns carry no id and never reorder — index is stable.
              // eslint-disable-next-line react/no-array-index-key
              <p key={i} className="truncate">
                <span className="font-medium text-muted-foreground">{message.role}: </span>
                {message.text}
              </p>
            ))
          )}
        </div>
      )}
    </li>
  );
}
