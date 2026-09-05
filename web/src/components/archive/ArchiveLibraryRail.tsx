import {
  ArchiveRestoreIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  EllipsisVerticalIcon,
  Trash2Icon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ArchiveLibraryToolbar,
  type ArchiveLibraryViewState,
  buildArchiveConversationFilters,
} from "@/components/archive/ArchiveLibraryToolbar";
import { ArchiveTranscriptViewer } from "@/components/archive/ArchiveTranscriptViewer";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  type Conversation,
  useArchiveConversation,
  useArchivedConversations,
  useArchivedSessionFacets,
  useStopAndDeleteConversation,
  useProjects,
} from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { cn } from "@/lib/utils";

const SPLIT_STORAGE_KEY = "omnigent:archive-library-rail-split-v1";
const DATE_STORAGE_KEY = "omnigent:archive-date-filter-v1";

function readDateSelection(): Pick<ArchiveLibraryViewState, "dateField" | "dateRange"> {
  try {
    const stored = JSON.parse(localStorage.getItem(DATE_STORAGE_KEY) ?? "null") as {
      dateField?: string;
      dateRange?: string;
    } | null;
    return {
      dateField:
        stored?.dateField === "created_at" || stored?.dateField === "active_at"
          ? stored.dateField
          : "archived_at",
      dateRange: typeof stored?.dateRange === "string" ? stored.dateRange : "",
    };
  } catch {
    return { dateField: "archived_at", dateRange: "" };
  }
}

function readSplit(): number {
  const parsed = Number.parseFloat(localStorage.getItem(SPLIT_STORAGE_KEY) ?? "");
  return Number.isFinite(parsed) ? Math.min(0.76, Math.max(0.24, parsed)) : 0.42;
}

function useVerticalSplit() {
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [ratio, setRatio] = useState(readSplit);
  const updateFromPointer = useCallback((clientY: number) => {
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds || bounds.height <= 0) return;
    setRatio(Math.min(0.76, Math.max(0.24, (clientY - bounds.top) / bounds.height)));
  }, []);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      if (dragging.current) updateFromPointer(event.clientY);
    };
    const onPointerUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      onPointerUp();
    };
  }, [updateFromPointer]);

  useEffect(() => localStorage.setItem(SPLIT_STORAGE_KEY, String(ratio)), [ratio]);

  return {
    containerRef,
    ratio,
    handleProps: {
      role: "separator" as const,
      tabIndex: 0,
      title: "Drag to resize the archive list and conversation; use arrow keys for small steps.",
      "aria-label": "Resize archive list and conversation",
      "aria-orientation": "horizontal" as const,
      "aria-valuemin": 24,
      "aria-valuemax": 76,
      "aria-valuenow": Math.round(ratio * 100),
      onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
        event.preventDefault();
        dragging.current = true;
        document.body.style.cursor = "row-resize";
        document.body.style.userSelect = "none";
        updateFromPointer(event.clientY);
      },
      onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        event.preventDefault();
        setRatio((current) =>
          Math.min(0.76, Math.max(0.24, current + (event.key === "ArrowDown" ? 0.04 : -0.04))),
        );
      },
    },
  };
}

function initialView(project?: string | null, hostId?: string | null): ArchiveLibraryViewState {
  return {
    searchQuery: "",
    searchScope: "title",
    project: project || undefined,
    hostId: hostId || undefined,
    agentName: undefined,
    ...readDateSelection(),
    sortField: "archived_at",
    order: "desc",
  };
}

function shortArchiveDate(conversation: Conversation): string {
  const timestamp = conversation.archived_at ?? conversation.updated_at ?? conversation.created_at;
  return new Date(timestamp * 1000).toLocaleDateString("en-US", {
    month: "2-digit",
    day: "2-digit",
  });
}

export function ArchiveLibraryRail({
  activeConversationId,
  initialProject,
  initialHostId,
}: {
  activeConversationId?: string;
  initialProject?: string | null;
  initialHostId?: string | null;
}) {
  const isMobileViewport = useIsMobileViewport();
  const { containerRef, ratio, handleProps } = useVerticalSplit();
  const [view, setView] = useState(() => initialView(initialProject, initialHostId));
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [pageAfter, setPageAfter] = useState<string | undefined>();
  const [pageHistory, setPageHistory] = useState<(string | undefined)[]>([]);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const facetTouched = useRef({ project: false, hostId: false });
  const activeIdRef = useRef(activeConversationId);
  const projects = useProjects();
  const hosts = useHosts({ includeSandbox: true });
  const archive = useArchiveConversation();
  const del = useStopAndDeleteConversation();

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedQuery(view.searchQuery.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [view.searchQuery]);

  useEffect(() => {
    localStorage.setItem(
      DATE_STORAGE_KEY,
      JSON.stringify({ dateField: view.dateField, dateRange: view.dateRange }),
    );
  }, [view.dateField, view.dateRange]);

  const filters = useMemo(
    () => buildArchiveConversationFilters(view, debouncedQuery),
    [debouncedQuery, view],
  );
  const facets = useArchivedSessionFacets(filters);
  const archivedQuery = useArchivedConversations(filters, pageAfter);
  const archived = useMemo(() => archivedQuery.data?.data ?? [], [archivedQuery.data]);
  const hostNames = useMemo(
    () => new Map((hosts.data ?? []).map((host) => [host.host_id, host.name])),
    [hosts.data],
  );
  const projectNamesById = useMemo(
    () => new Map((projects.data ?? []).map((project) => [project.id, project.name])),
    [projects.data],
  );
  const projectOptions = useMemo(
    () => (facets.data?.projects ?? []).map((name) => ({ value: name, label: name })),
    [facets.data],
  );
  const hostOptions = useMemo(
    () =>
      (facets.data?.hostIds ?? []).map((hostId) => ({
        value: hostId,
        label: hostNames.get(hostId) ?? hostId,
        keywords: hostId,
      })),
    [facets.data, hostNames],
  );
  const agentOptions = useMemo(
    () => (facets.data?.agentNames ?? []).map((name) => ({ value: name, label: name })),
    [facets.data],
  );

  useEffect(() => {
    if (activeIdRef.current !== activeConversationId) {
      activeIdRef.current = activeConversationId;
      facetTouched.current = { project: false, hostId: false };
      setView((current) => ({
        ...current,
        project: initialProject || undefined,
        hostId: initialHostId || undefined,
      }));
      return;
    }
    setView((current) => {
      const project =
        !facetTouched.current.project && initialProject ? initialProject : current.project;
      const hostId = !facetTouched.current.hostId && initialHostId ? initialHostId : current.hostId;
      return project === current.project && hostId === current.hostId
        ? current
        : { ...current, project, hostId };
    });
  }, [activeConversationId, initialHostId, initialProject]);

  useEffect(() => {
    if (!facets.data) return;
    setView((current) => {
      const next = { ...current };
      let changed = false;
      const normalize = (key: "project" | "hostId" | "agentName", options: { value: string }[]) => {
        const selectedValue = next[key];
        if (selectedValue && !options.some((option) => option.value === selectedValue)) {
          next[key] = undefined;
          changed = true;
        }
      };
      normalize("project", projectOptions);
      normalize("hostId", hostOptions);
      normalize("agentName", agentOptions);
      return changed ? next : current;
    });
  }, [agentOptions, facets.data, hostOptions, projectOptions]);

  useEffect(() => {
    setPageAfter(undefined);
    setPageHistory([]);
  }, [filters]);

  useEffect(() => {
    if (archivedQuery.isLoading) return;
    const current = archived.find((conversation) => conversation.id === selected?.id);
    setSelected(current ?? (isMobileViewport ? null : archived[0]) ?? null);
  }, [archived, archivedQuery.isLoading, isMobileViewport, selected?.id]);

  const changeView = useCallback((patch: Partial<ArchiveLibraryViewState>) => {
    if (Object.hasOwn(patch, "project")) {
      facetTouched.current.project = true;
    }
    if (Object.hasOwn(patch, "hostId")) {
      facetTouched.current.hostId = true;
    }
    setPageAfter(undefined);
    setPageHistory([]);
    setView((current) => ({ ...current, ...patch }));
  }, []);

  if (isMobileViewport && selected) {
    return (
      <ArchiveTranscriptViewer
        conversation={selected}
        onBack={() => setSelected(null)}
        className="h-full w-full"
      />
    );
  }

  return (
    <div
      ref={containerRef}
      className="flex min-h-0 flex-1 flex-col"
      data-testid="archive-library-rail"
    >
      <section
        className="flex min-h-0 shrink-0 flex-col"
        style={isMobileViewport ? { height: "100%" } : { height: `${ratio * 100}%` }}
      >
        <ArchiveLibraryToolbar
          value={view}
          projectOptions={projectOptions}
          hostOptions={hostOptions}
          agentOptions={agentOptions}
          onChange={changeView}
        />
        <div className="flex h-6 shrink-0 items-center border-b px-2 text-[10px] text-muted-foreground">
          <span>
            {archived.length} sessions
            {view.searchScope === "content" && debouncedQuery ? " · content matches" : ""}
          </span>
        </div>
        <div
          ref={listRef}
          className="min-h-0 flex-1 overflow-y-auto p-1"
          role="listbox"
          aria-label="Archived sessions"
        >
          {archivedQuery.isLoading ? (
            <p className="p-2 text-xs text-muted-foreground">Loading…</p>
          ) : archived.length === 0 ? (
            <p className="p-2 text-xs text-muted-foreground">No archived sessions match.</p>
          ) : (
            archived.map((conversation, index) => {
              const projectName =
                (conversation.project_id
                  ? projectNamesById.get(conversation.project_id)
                  : undefined) ?? conversation.labels?.omni_project;
              const busy = archive.isPending || del.isPending;
              return (
                <div
                  key={conversation.id}
                  className={cn(
                    "group flex min-h-12 items-center rounded-md border border-transparent hover:bg-muted",
                    selected?.id === conversation.id && "border-primary/30 bg-primary/5",
                  )}
                >
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected?.id === conversation.id}
                    className="min-w-0 flex-1 px-2 py-1.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={() => setSelected(conversation)}
                    onKeyDown={(event) => {
                      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                        event.preventDefault();
                        const rows =
                          listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]');
                        const next = event.key === "ArrowDown" ? index + 1 : index - 1;
                        rows?.[Math.max(0, Math.min(archived.length - 1, next))]?.focus();
                      }
                      if (event.key === "Enter") {
                        setSelected(conversation);
                        window.setTimeout(
                          () =>
                            document
                              .querySelector<HTMLElement>('[data-testid="archive-transcript"]')
                              ?.focus(),
                          0,
                        );
                      }
                    }}
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="min-w-0 flex-1 truncate text-xs font-medium">
                        {conversationDisplayLabel(conversation)}
                      </span>
                      {projectName && (
                        <span className="max-w-24 shrink-0 truncate rounded bg-muted px-1 text-[9px] text-muted-foreground">
                          {projectName}
                        </span>
                      )}
                    </span>
                    <span className="mt-1 block truncate text-[10px] text-muted-foreground">
                      {hostNames.get(conversation.host_id ?? "") ??
                        conversation.host_id ??
                        "Host not recorded"}
                      {conversation.agent_name ? ` · ${conversation.agent_name}` : ""}
                      {` · Archived ${shortArchiveDate(conversation)}`}
                      {view.searchScope === "content" && conversation.search_match
                        ? ` · ● ${conversation.search_match_count ?? 1} match${(conversation.search_match_count ?? 1) === 1 ? "" : "es"}`
                        : ""}
                    </span>
                  </button>
                  <div className="hidden shrink-0 items-center pr-1 md:flex">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          className="max-md:size-11"
                          aria-label="Unarchive session"
                          disabled={busy}
                          onClick={() => archive.mutate({ id: conversation.id, archived: false })}
                        >
                          <ArchiveRestoreIcon className="size-4" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Unarchive</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          className="max-md:size-11"
                          aria-label="Delete archived session"
                          disabled={busy}
                          onClick={() => setDeleteTarget(conversation)}
                        >
                          <Trash2Icon className="size-4 text-destructive" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Delete permanently</TooltipContent>
                    </Tooltip>
                  </div>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        className="mr-1 size-11 shrink-0 md:hidden"
                        aria-label="Archived session actions"
                        disabled={busy}
                      >
                        <EllipsisVerticalIcon className="size-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem
                        onSelect={() => archive.mutate({ id: conversation.id, archived: false })}
                      >
                        <ArchiveRestoreIcon className="size-4" />
                        Unarchive
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        variant="destructive"
                        onSelect={() => setDeleteTarget(conversation)}
                      >
                        <Trash2Icon className="size-4" />
                        Delete
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              );
            })
          )}
        </div>
        {(pageHistory.length > 0 || archivedQuery.data?.has_more) && (
          <div className="flex h-8 shrink-0 items-center justify-between gap-2 border-t px-2">
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={pageHistory.length === 0 || archivedQuery.isFetching}
              onClick={() => {
                const previous = pageHistory.at(-1);
                setPageHistory((current) => current.slice(0, -1));
                setPageAfter(previous);
              }}
            >
              <ChevronLeftIcon /> Previous
            </Button>
            <span className="text-[10px] text-muted-foreground">Page {pageHistory.length + 1}</span>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={!archivedQuery.data?.has_more || archivedQuery.isFetching}
              onClick={() => {
                const next = archivedQuery.data?.last_id;
                if (!next) return;
                setPageHistory((current) => [...current, pageAfter]);
                setPageAfter(next);
              }}
            >
              Next <ChevronRightIcon />
            </Button>
          </div>
        )}
      </section>
      {!isMobileViewport && (
        <>
          <div
            {...handleProps}
            className="group relative h-1 shrink-0 cursor-row-resize bg-border/80 outline-none focus-visible:bg-primary/50"
          >
            <span className="absolute top-1/2 left-1/2 h-1 w-10 -translate-x-1/2 -translate-y-1/2 rounded-full bg-border-strong group-hover:bg-primary/50" />
          </div>
          <ArchiveTranscriptViewer conversation={selected} returnFocusRef={listRef} />
        </>
      )}
      <Dialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete archived session?</DialogTitle>
            <DialogDescription>
              {deleteTarget ? conversationDisplayLabel(deleteTarget) : "This session"} and its
              history will be permanently removed.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={del.isPending}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={del.isPending || deleteTarget === null}
              onClick={() => {
                if (!deleteTarget) return;
                del.mutate({ id: deleteTarget.id });
                setDeleteTarget(null);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
