import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@/lib/routing";
import { FolderIcon, MessageSquareIcon, XIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ApiError } from "@/lib/sessionsApi";
import {
  IMPORT_SOURCES,
  importLocalSession,
  listLocalImportSessions,
  type ImportSource,
  type LocalImportSession,
} from "@/lib/importsApi";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { relativeTime } from "@/lib/relativeTime";

const RECENT_SESSION_LIMIT = 20;

/** Vendor name for a source, e.g. `"claude"` → `"Claude Code"`. */
function sourceLabel(source: ImportSource): string {
  return nativeCodingAgentForHarness(`${source}-native`)?.displayName ?? source;
}

/** Last path segment of a workspace, e.g. `"/Users/x/repo"` → `"repo"`. */
function workspaceName(workspace: string): string {
  const segments = workspace.split("/").filter(Boolean);
  return segments[segments.length - 1] ?? workspace;
}

function SessionRow({ session, onSelect }: { session: LocalImportSession; onSelect: () => void }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={`import-chat-session-${session.sessionId}`}
      className="flex w-full flex-col gap-1 rounded-md px-3 py-2 text-left transition-colors hover:bg-muted"
    >
      <span className="truncate text-sm font-medium">{session.title ?? session.sessionId}</span>
      <span className="flex items-center gap-3 text-xs text-muted-foreground">
        {session.workspace !== null && (
          <span className="flex min-w-0 items-center gap-1" title={session.workspace}>
            <FolderIcon className="size-3 shrink-0" />
            <span className="truncate">{workspaceName(session.workspace)}</span>
          </span>
        )}
        <span className="flex shrink-0 items-center gap-1">
          <MessageSquareIcon className="size-3" />
          {session.itemCount}
        </span>
        {session.modifiedAt !== null && (
          <span className="shrink-0">{relativeTime(session.modifiedAt * 1000)}</span>
        )}
      </span>
    </button>
  );
}

/**
 * Dialog for pulling an existing local coding-harness chat into Omnigent —
 * the web equivalent of `omnigent import`.
 *
 * The transcripts live on the SERVER's disk (that machine's `~/.claude`,
 * `~/.codex`, …), so the list is only meaningful on a single-user local
 * server; the trigger is hidden elsewhere and the API refuses it anyway.
 * Picking a harness lists its recent chats, selecting one collapses the list
 * to that chat, and importing navigates into the new Omnigent session.
 *
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Radix-controlled visibility setter.
 */
export function ImportChatDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [source, setSource] = useState<ImportSource>("claude");
  const [selected, setSelected] = useState<LocalImportSession | null>(null);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set when the chosen chat was already imported: the only error the user can
  // act on from here, by re-importing over the previous copy.
  const [conflict, setConflict] = useState<string | null>(null);

  const {
    data: sessions,
    isLoading,
    error: listError,
  } = useQuery({
    queryKey: ["local-import-sessions", source],
    queryFn: () => listLocalImportSessions(source, RECENT_SESSION_LIMIT),
    enabled: open,
    staleTime: 4_000,
  });

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelected(null);
      setImporting(false);
      setError(null);
      setConflict(null);
    }
    onOpenChange(next);
  }

  function handleSelectSource(next: ImportSource): void {
    setSource(next);
    setSelected(null);
    setError(null);
    setConflict(null);
  }

  function handleSelectSession(session: LocalImportSession): void {
    setSelected(session);
    setError(null);
    setConflict(null);
  }

  async function handleImport(force: boolean): Promise<void> {
    if (selected === null) return;
    setImporting(true);
    setError(null);
    setConflict(null);
    try {
      const imported = await importLocalSession({
        source,
        externalSessionId: selected.sessionId,
        force,
      });
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      handleOpenChange(false);
      navigate(`/c/${imported.sessionId}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setConflict(e.message);
      } else {
        setError(e instanceof Error ? e.message : "Couldn't import the chat. Try again.");
      }
    } finally {
      setImporting(false);
    }
  }

  const label = sourceLabel(source);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="import-chat-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Import a chat</DialogTitle>
          <DialogDescription>
            Bring an existing chat from a coding agent on this machine into Omnigent. The transcript
            is copied as an ordinary session — the original is left untouched.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium text-muted-foreground">Coding agent</span>
          <Select value={source} onValueChange={(v) => handleSelectSource(v as ImportSource)}>
            <SelectTrigger className="w-full text-sm" data-testid="import-chat-source-select">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {IMPORT_SOURCES.map((option) => (
                <SelectItem
                  key={option}
                  value={option}
                  data-testid={`import-chat-source-option-${option}`}
                >
                  {sourceLabel(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {selected !== null ? (
          <div
            className="flex items-start justify-between gap-2 rounded-md border border-input px-3 py-2"
            data-testid="import-chat-selected"
          >
            <span className="flex min-w-0 flex-col gap-1">
              <span className="truncate text-sm font-medium">
                {selected.title ?? selected.sessionId}
              </span>
              {selected.workspace !== null && (
                <span className="truncate text-xs text-muted-foreground" title={selected.workspace}>
                  {selected.workspace}
                </span>
              )}
            </span>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-8 shrink-0 text-muted-foreground"
              onClick={() => setSelected(null)}
              data-testid="import-chat-clear-selection"
            >
              <XIcon className="size-4" />
              <span className="sr-only">Choose a different chat</span>
            </Button>
          </div>
        ) : isLoading ? (
          <div className="flex flex-col gap-2" data-testid="import-chat-loading">
            {[0, 1, 2].map((row) => (
              <div key={row} className="h-11 animate-pulse rounded-md bg-muted" />
            ))}
          </div>
        ) : listError !== null ? (
          <p className="text-sm text-destructive" data-testid="import-chat-list-error">
            {listError instanceof Error ? listError.message : "Couldn't list local chats."}
          </p>
        ) : (sessions ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="import-chat-empty">
            No recent {label} chats found on this machine.
          </p>
        ) : (
          <ScrollArea className="max-h-64" data-testid="import-chat-session-list">
            <div className="flex flex-col">
              {(sessions ?? []).map((session) => (
                <SessionRow
                  key={session.sessionId}
                  session={session}
                  onSelect={() => handleSelectSession(session)}
                />
              ))}
            </div>
          </ScrollArea>
        )}

        {conflict !== null && (
          <p className="text-sm text-warning" data-testid="import-chat-conflict">
            {conflict}{" "}
            <button
              type="button"
              className="underline underline-offset-2"
              disabled={importing}
              onClick={() => void handleImport(true)}
              data-testid="import-chat-force"
            >
              Import again?
            </button>
          </p>
        )}
        {error !== null && (
          <p className="text-sm text-destructive" data-testid="import-chat-error">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button
            type="button"
            disabled={selected === null || importing}
            onClick={() => void handleImport(false)}
            data-testid="import-chat-submit"
          >
            {importing ? "Importing…" : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
