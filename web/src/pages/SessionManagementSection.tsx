/**
 * Settings → Session management.
 *
 * Lists active (non-archived) sessions with search + project filters and
 * multi-select archive / delete. Shared / non-owned sessions stay visible
 * but cannot be selected — only the owner can archive or hard-delete.
 *
 * “Select all” covers eligible rows currently loaded under the active
 * filters, not unloaded pages. Bulk mutations reuse the existing
 * per-session parallel hooks (partial success is expected).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  ArchiveIcon,
  Loader2Icon,
  SquareCheckIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react";
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  PROJECT_LABEL_KEY,
  type Conversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useConversations,
  useProjects,
} from "@/hooks/useConversations";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { absoluteTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel, filterConversations } from "@/shell/sidebarNav";
import { computeShiftSelectRange } from "@/lib/shiftSelect";

const SEARCH_DEBOUNCE_MS = 300;

// Discriminated Select values so the "no filter" sentinel can never collide
// with a real project name (same scheme as ArchivedSection).
const ALL_PROJECTS_VALUE = "all";
const PROJECT_VALUE_PREFIX = "project:";

function projectToSelectValue(project: string | undefined): string {
  return project === undefined ? ALL_PROJECTS_VALUE : PROJECT_VALUE_PREFIX + project;
}

function selectValueToProject(value: string): string | undefined {
  if (value === ALL_PROJECTS_VALUE) return undefined;
  return value.slice(PROJECT_VALUE_PREFIX.length);
}

function isOwnedByViewer(conversation: Conversation): boolean {
  return isOwnerLevel(conversation.permission_level);
}

type BulkFailure = {
  action: "archive" | "delete";
  failed: string[];
  total: number;
};

export function SessionManagementSection() {
  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [project, setProject] = useState<string | undefined>(undefined);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [bulkFailure, setBulkFailure] = useState<BulkFailure | null>(null);

  const lastSelectedIdRef = useRef<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const projectsQuery = useProjects();
  const projectNames = useMemo(() => projectsQuery.data ?? [], [projectsQuery.data]);

  // Drop a stale project filter once the projects list settles without it.
  useEffect(() => {
    if (
      project !== undefined &&
      projectsQuery.isSuccess &&
      !projectsQuery.isFetching &&
      !projectNames.includes(project)
    ) {
      setProject(undefined);
    }
  }, [project, projectNames, projectsQuery.isSuccess, projectsQuery.isFetching]);

  const listQuery = useConversations(debouncedSearch, false, undefined, project);
  // Server `search_query` also matches message bodies (command-palette
  // behaviour). Session management is a cleanup surface, so keep only
  // title / id hits — otherwise a chat that once said "Hi" keeps a
  // differently-titled session in the filtered list.
  const sessions = useMemo(() => {
    const active = (listQuery.data?.pages ?? [])
      .flatMap((p) => p.data)
      .filter((c) => c.archived !== true);
    return filterConversations(active, debouncedSearch);
  }, [listQuery.data, debouncedSearch]);

  // Keep a just-cleared project listed while the refetch settles.
  const projectItems =
    project && !projectNames.includes(project) ? [project, ...projectNames] : projectNames;

  const ownedSessions = useMemo(() => sessions.filter(isOwnedByViewer), [sessions]);
  const ownedIds = useMemo(() => ownedSessions.map((c) => c.id), [ownedSessions]);
  const ownedIdSet = useMemo(() => new Set(ownedIds), [ownedIds]);

  // Drop selection entries that are no longer in the loaded owned set
  // (filter change, pagination shrink, successful mutation).
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (ownedIdSet.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [ownedIdSet]);

  const selectedOwned = useMemo(
    () => ownedSessions.filter((c) => selectedIds.has(c.id)),
    [ownedSessions, selectedIds],
  );

  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();
  const isBusy = bulkArchive.isPending || bulkDelete.isPending;

  const allEligibleSelected =
    ownedIds.length > 0 && ownedIds.every((id) => selectedIds.has(id)) && selectedIds.size > 0;

  const toggleSelected = useCallback(
    (id: string, shiftKey?: boolean) => {
      if (!ownedIdSet.has(id)) return;
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (shiftKey && lastSelectedIdRef.current != null) {
          const range = computeShiftSelectRange(ownedIds, lastSelectedIdRef.current, id);
          if (range) {
            for (const rid of range) {
              if (ownedIdSet.has(rid)) next.add(rid);
            }
            return next;
          }
        }
        if (next.has(id)) next.delete(id);
        else next.add(id);
        lastSelectedIdRef.current = id;
        return next;
      });
      setBulkFailure(null);
    },
    [ownedIdSet, ownedIds],
  );

  const selectAllEligible = useCallback(() => {
    setSelectedIds(new Set(ownedIds));
    setBulkFailure(null);
  }, [ownedIds]);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
    setBulkFailure(null);
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
    setBulkFailure(null);
  }, []);

  function applyPartialSuccess(failed: string[], succeeded?: string[]) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (succeeded) {
        for (const id of succeeded) next.delete(id);
      } else {
        // Archive path: keep only failed IDs that were in the selection.
        for (const id of [...next]) {
          if (!failed.includes(id)) next.delete(id);
        }
      }
      // Ensure failed IDs stay selected for retry.
      for (const id of failed) {
        if (ownedIdSet.has(id)) next.add(id);
      }
      return next;
    });
  }

  function handleArchive() {
    const ids = selectedOwned.map((c) => c.id);
    if (ids.length === 0) return;
    setBulkFailure(null);
    bulkArchive.mutate(
      { ids, archived: true },
      {
        onSuccess: () => {
          deselectAll();
        },
        onError: (err: unknown) => {
          const failure = err as { failed?: string[]; total?: number };
          if (failure?.failed?.length) {
            applyPartialSuccess(failure.failed);
            setBulkFailure({
              action: "archive",
              failed: failure.failed,
              total: failure.total ?? ids.length,
            });
          }
        },
      },
    );
  }

  function handleDelete() {
    const ids = selectedOwned.map((c) => c.id);
    if (ids.length === 0) return;
    setConfirmDeleteOpen(false);
    setBulkFailure(null);
    bulkDelete.mutate(ids, {
      onSuccess: () => {
        deselectAll();
      },
      onError: (err: unknown) => {
        const failure = err as { failed?: string[]; succeeded?: string[]; total?: number };
        if (failure?.failed?.length) {
          applyPartialSuccess(failure.failed, failure.succeeded);
          setBulkFailure({
            action: "delete",
            failed: failure.failed,
            total: failure.total ?? ids.length,
          });
        }
      },
    });
  }

  function retryFailed() {
    if (!bulkFailure) return;
    const ids = bulkFailure.failed.filter((id) => ownedIdSet.has(id));
    if (ids.length === 0) {
      setBulkFailure(null);
      return;
    }
    setSelectedIds(new Set(ids));
    if (bulkFailure.action === "archive") {
      setBulkFailure(null);
      bulkArchive.mutate(
        { ids, archived: true },
        {
          onSuccess: () => deselectAll(),
          onError: (err: unknown) => {
            const failure = err as { failed?: string[]; total?: number };
            if (failure?.failed?.length) {
              applyPartialSuccess(failure.failed);
              setBulkFailure({
                action: "archive",
                failed: failure.failed,
                total: failure.total ?? ids.length,
              });
            }
          },
        },
      );
    } else {
      setBulkFailure(null);
      setConfirmDeleteOpen(true);
    }
  }

  const count = selectedIds.size;
  const emptyMessage = project
    ? "No active sessions in this project."
    : debouncedSearch
      ? "No sessions match your search."
      : "No active sessions.";

  return (
    <section>
      <h1 className="text-2xl font-semibold">Session management</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Archive or permanently delete active sessions in bulk. Search matches session titles.
        Shared sessions you don’t own stay visible but can’t be selected.
      </p>

      <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1">
          <label
            htmlFor="session-mgmt-search"
            className="mb-1.5 block text-sm text-muted-foreground"
          >
            Search
          </label>
          <Input
            id="session-mgmt-search"
            data-testid="session-mgmt-search"
            type="search"
            placeholder="Search by title…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        {(projectItems.length > 0 || project) && (
          <div className="shrink-0">
            <label
              htmlFor="session-mgmt-project-filter"
              className="mb-1.5 block text-sm text-muted-foreground"
            >
              Project
            </label>
            <Select
              value={projectToSelectValue(project)}
              onValueChange={(value) => setProject(selectValueToProject(value))}
            >
              <SelectTrigger
                id="session-mgmt-project-filter"
                aria-label="Filter sessions by project"
                data-testid="session-mgmt-project-filter"
                className="w-56"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={ALL_PROJECTS_VALUE}>All projects</SelectItem>
                {projectItems.map((name) => (
                  <SelectItem
                    key={name}
                    value={projectToSelectValue(name)}
                    data-testid={`session-mgmt-project-option-${name}`}
                  >
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>

      <div
        className="mt-4 flex flex-wrap items-center gap-1.5"
        data-testid="session-mgmt-action-bar"
      >
        <span className="mr-1 shrink-0 text-sm text-muted-foreground">
          {count === 0 ? "None selected" : `${count} selected`}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-1.5 text-sm"
          data-testid="session-mgmt-select-all"
          disabled={ownedIds.length === 0 || isBusy}
          onClick={allEligibleSelected ? deselectAll : selectAllEligible}
        >
          {allEligibleSelected ? "Deselect all" : "Select all"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-1.5 text-sm"
          data-testid="session-mgmt-clear"
          disabled={count === 0 || isBusy}
          onClick={clearSelection}
        >
          Clear
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-7 gap-1.5 text-xs"
          data-testid="session-mgmt-archive"
          disabled={isBusy || selectedOwned.length === 0}
          onClick={handleArchive}
        >
          {bulkArchive.isPending ? (
            <Loader2Icon className="size-3 animate-spin" />
          ) : (
            <ArchiveIcon className="size-3" />
          )}
          Archive
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={cn("h-7 gap-1.5 text-xs", selectedOwned.length > 0 && "text-destructive")}
          data-testid="session-mgmt-delete"
          disabled={isBusy || selectedOwned.length === 0}
          onClick={() => setConfirmDeleteOpen(true)}
        >
          {bulkDelete.isPending ? (
            <Loader2Icon className="size-3 animate-spin" />
          ) : (
            <Trash2Icon className="size-3" />
          )}
          Delete{selectedOwned.length > 0 ? ` ${selectedOwned.length}` : ""}
        </Button>
      </div>

      {bulkFailure && (
        <div
          className="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="session-mgmt-bulk-error"
        >
          <span>
            {bulkFailure.failed.length} of {bulkFailure.total}{" "}
            {bulkFailure.action === "archive" ? "archive" : "delete"} action
            {bulkFailure.total === 1 ? "" : "s"} failed.
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-1.5 text-destructive"
            data-testid="session-mgmt-retry"
            disabled={isBusy}
            onClick={retryFailed}
          >
            Retry
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-1.5"
            data-testid="session-mgmt-dismiss-error"
            disabled={isBusy}
            onClick={() => setBulkFailure(null)}
          >
            Dismiss
          </Button>
        </div>
      )}

      <div className="mt-4">
        {listQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : listQuery.isError ? (
          <p className="text-sm text-destructive" role="alert">
            Failed to load sessions.
          </p>
        ) : sessions.length === 0 && !listQuery.hasNextPage ? (
          <p className="text-sm text-muted-foreground">{emptyMessage}</p>
        ) : (
          <>
            {sessions.length > 0 && (
              <ul className="flex flex-col gap-0.5" data-testid="session-mgmt-list">
                {sessions.map((conv) => (
                  <SessionRow
                    key={conv.id}
                    conversation={conv}
                    selected={selectedIds.has(conv.id)}
                    disabled={!isOwnedByViewer(conv) || isBusy}
                    onToggle={toggleSelected}
                  />
                ))}
              </ul>
            )}
            {sessions.length === 0 && listQuery.hasNextPage && (
              <p className="text-sm text-muted-foreground">No active sessions on this page.</p>
            )}
            {listQuery.hasNextPage && (
              <div className="mt-3">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  data-testid="session-mgmt-load-more"
                  disabled={listQuery.isFetchingNextPage}
                  onClick={() => void listQuery.fetchNextPage()}
                >
                  {listQuery.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            )}
          </>
        )}
      </div>

      <Dialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {selectedOwned.length} session(s)?</DialogTitle>
            <DialogDescription>
              This will permanently delete the selected sessions and all their history. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs text-muted-foreground">
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0 text-warning" />
            Branches are not cleaned up. Use single-session delete for branch surgery.
          </p>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmDeleteOpen(false)}
              disabled={bulkDelete.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="session-mgmt-confirm-delete"
              onClick={handleDelete}
              disabled={bulkDelete.isPending || selectedOwned.length === 0}
            >
              Delete {selectedOwned.length} session(s)
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function SessionRow({
  conversation,
  selected,
  disabled,
  onToggle,
}: {
  conversation: Conversation;
  selected: boolean;
  disabled: boolean;
  onToggle: (id: string, shiftKey?: boolean) => void;
}) {
  const owned = isOwnedByViewer(conversation);
  const label = conversationDisplayLabel(conversation);
  const project = conversation.labels?.[PROJECT_LABEL_KEY];

  return (
    <li
      data-testid="session-mgmt-row"
      data-session-id={conversation.id}
      data-owned={owned ? "true" : "false"}
      title={
        owned ? undefined : "You don’t own this session, so it can’t be archived or deleted here."
      }
      className={cn(
        "group relative flex items-center gap-2 rounded-md px-3 py-2",
        owned && !disabled && "cursor-pointer hover:bg-muted",
        selected && "bg-primary/5",
        !owned && "opacity-70",
      )}
      onClick={(e) => {
        if (!owned || disabled) return;
        onToggle(conversation.id, e.shiftKey);
      }}
      onKeyDown={(e) => {
        if (!owned || disabled) return;
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          onToggle(conversation.id, e.shiftKey);
        }
      }}
      // Owned rows are interactive; shared rows are display-only.
      {...(owned
        ? {
            role: "checkbox" as const,
            tabIndex: disabled ? -1 : 0,
            "aria-checked": selected,
            "aria-disabled": disabled || undefined,
            "aria-label": label,
          }
        : {})}
    >
      <span className="flex size-4 shrink-0 items-center justify-center" aria-hidden>
        {owned ? (
          selected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )
        ) : (
          <SquareIcon className="size-4 text-muted-foreground/40" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium" title={label}>
          {label}
        </div>
        <div className="truncate text-xs text-muted-foreground">
          {project ? `${project} · ` : ""}
          {absoluteTime(conversation.updated_at * 1000)}
          {!owned ? " · Shared with you" : ""}
        </div>
      </div>
    </li>
  );
}
