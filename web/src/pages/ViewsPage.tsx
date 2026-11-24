import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRightIcon,
  LayersIcon,
  LockKeyholeIcon,
  PlusIcon,
  StarIcon,
  Trash2Icon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { useProjects } from "@/hooks/useConversations";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useNavigate, useParams, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import {
  deleteWorkView,
  getWorkView,
  listWorkViews,
  newView,
  saveWorkView,
  type SavedWorkView,
  type ViewEntity,
  type ViewInput,
  type ViewRecord,
} from "@/lib/workViewsApi";
import { EntityTabs, ViewControls } from "@/views/ViewControls";
import { QueriedViewResults } from "@/views/QueriedViewResults";
import { WorkTaskDialog } from "@/views/WorkTaskDialog";

function viewInput(view: SavedWorkView): ViewInput {
  const { name, description, favorite, entity_type, filters, display } = view;
  return { name, description, favorite, entity_type, filters, display };
}

export function ViewsPage() {
  const info = useServerInfo();
  const { viewId } = useParams<{ viewId: string }>();
  if (info === "loading")
    return (
      <PageScroll>
        <Spinner aria-label="Loading views" />
      </PageScroll>
    );
  if (!info.work_views)
    return (
      <PageScroll>
        <p>Views are unavailable on this server.</p>
      </PageScroll>
    );
  return (
    <PageScroll maxWidthClassName="max-w-none" extraTop="0.5rem" contentClassName="px-3 sm:px-6">
      <div className="min-h-[70vh] rounded-2xl border border-border/60 bg-background/80">
        {viewId ? <ViewDetail key={viewId} viewId={viewId} /> : <ViewsLibrary />}
      </div>
    </PageScroll>
  );
}

function ViewsLibrary() {
  const [entity, setEntity] = useState<ViewEntity>("tasks");
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<SavedWorkView | null>(null);
  const queryClient = useQueryClient();
  const views = useInfiniteQuery({
    queryKey: ["work-views"],
    queryFn: ({ pageParam }) => listWorkViews(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteWorkView(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["work-views"] });
      setDeleting(null);
    },
  });
  const favorite = useMutation({
    mutationFn: (view: SavedWorkView) =>
      saveWorkView({ ...viewInput(view), favorite: !view.favorite }, view.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["work-views"] }),
  });
  const items = (views.data?.pages.flatMap((page) => page.data) ?? [])
    .filter(
      (view) =>
        view.entity_type === entity &&
        `${view.name} ${view.description}`.toLowerCase().includes(search.toLowerCase()),
    )
    .sort(
      (left, right) =>
        Number(right.favorite) - Number(left.favorite) || left.name.localeCompare(right.name),
    );
  return (
    <>
      <header className="flex items-center justify-between border-b border-border/60 px-5 py-4">
        <h1 className="text-lg font-semibold">Views</h1>
        <Button asChild variant="ghost">
          <Link to={`/views/new?type=${entity}`}>
            <PlusIcon />
            New view
          </Link>
        </Button>
      </header>
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <EntityTabs value={entity} onChange={setEntity} />
        <Input
          aria-label="Search saved views"
          placeholder="Search views…"
          className="w-48 border-transparent bg-muted/40"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>
      <div className="px-5 pb-6">
        <div className="mb-2 flex items-center px-3 py-2 text-xs text-muted-foreground">
          <span>Name</span>
          <span className="ml-auto mr-24">Owner</span>
        </div>
        <div className="mb-2 flex items-center gap-2 rounded-lg bg-muted/50 px-3 py-2 text-sm font-medium">
          <LockKeyholeIcon className="size-4 text-muted-foreground" />
          Personal views
        </div>
        {views.isPending ? (
          <div className="flex justify-center p-10">
            <Spinner aria-label="Loading saved views" />
          </div>
        ) : views.error ? (
          <p role="alert" className="p-6 text-sm text-destructive">
            {views.error.message}
            <Button variant="ghost" onClick={() => void views.refetch()}>
              Retry
            </Button>
          </p>
        ) : (
          <>
            {items.map((view) => (
              <div
                key={view.id}
                className="group flex items-center gap-3 rounded-lg px-3 py-4 hover:bg-muted/40"
              >
                <LayersIcon className="size-5 shrink-0 text-muted-foreground" />
                <Link to={`/views/${view.id}`} className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{view.name}</span>
                  {view.description && (
                    <span className="mt-1 block truncate text-sm text-muted-foreground">
                      {view.description}
                    </span>
                  )}
                </Link>
                <span className="hidden text-sm text-muted-foreground sm:inline">You</span>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`${view.favorite ? "Unfavorite" : "Favorite"} ${view.name}`}
                  disabled={favorite.isPending}
                  onClick={() => favorite.mutate(view)}
                >
                  <StarIcon className={cn(view.favorite && "fill-amber-400 text-amber-500")} />
                </Button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`Delete ${view.name}`}
                  onClick={() => setDeleting(view)}
                >
                  <Trash2Icon />
                </Button>
              </div>
            ))}
            {items.length === 0 && (
              <div className="flex flex-col items-center gap-3 px-5 py-16 text-center">
                <LayersIcon className="size-8 text-muted-foreground/60" />
                <h2 className="text-base font-medium">
                  {search ? "No matching views" : `Your ${entity}, your perspective`}
                </h2>
                <p className="max-w-sm text-sm text-muted-foreground">
                  {search
                    ? "Try a different name or load more views."
                    : "Choose filters and a layout, then save a view to return to it."}
                </p>
                <Button asChild variant="outline">
                  <Link to={`/views/new?type=${entity}`}>Explore {entity}</Link>
                </Button>
              </div>
            )}
            {views.hasNextPage && (
              <Button
                variant="ghost"
                disabled={views.isFetchingNextPage}
                onClick={() => void views.fetchNextPage()}
              >
                Load more views
              </Button>
            )}
          </>
        )}
        {favorite.error && (
          <p role="alert" className="text-sm text-destructive">
            {favorite.error.message}
          </p>
        )}
      </div>
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDeleting(null);
            remove.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name}?</DialogTitle>
            <DialogDescription>
              The saved view will be removed. Its tasks, projects and sessions will remain.
            </DialogDescription>
          </DialogHeader>
          {remove.error && (
            <p role="alert" className="text-sm text-destructive">
              {remove.error.message}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => {
                if (deleting) remove.mutate(deleting.id);
              }}
            >
              Delete view
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ViewDetail({ viewId }: { viewId: string }) {
  const [searchParams] = useSearchParams();
  const saved = useQuery({
    queryKey: ["work-view", viewId],
    queryFn: () => getWorkView(viewId),
    enabled: viewId !== "new",
  });
  if (viewId !== "new" && saved.isPending)
    return (
      <div className="flex justify-center p-12">
        <Spinner aria-label="Loading view" />
      </div>
    );
  if (saved.error)
    return (
      <div className="space-y-3 p-8">
        <p role="alert" className="text-destructive">
          {saved.error.message}
        </p>
        <Button variant="outline" onClick={() => void saved.refetch()}>
          Retry
        </Button>
        <Button asChild variant="ghost">
          <Link to="/views">Back to views</Link>
        </Button>
      </div>
    );
  const editorKey =
    viewId === "new" ? `${searchParams.get("type")}:${searchParams.get("project")}` : viewId;
  return <ViewEditor key={editorKey} saved={saved.data} />;
}

function ViewEditor({ saved }: { saved?: SavedWorkView }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const projects = useProjects();
  const projectOptions = (projects.data ?? []).flatMap((project) =>
    project.id ? [{ id: project.id, name: project.name }] : [],
  );
  const [value, setValue] = useState<ViewInput>(() => {
    if (saved) return viewInput(saved);
    const requestedType = searchParams.get("type");
    const initial = newView(
      requestedType === "projects" || requestedType === "sessions" ? requestedType : "tasks",
    );
    const project = searchParams.get("project");
    if (project && /^[0-9a-f]{32}$/.test(project)) initial.filters.project_ids = [project];
    return initial;
  });
  const [baseline, setBaseline] = useState(() => JSON.stringify(saved ? viewInput(saved) : null));
  const save = useMutation({
    mutationFn: () => saveWorkView(value, saved?.id),
    onSuccess: async (view) => {
      setBaseline(JSON.stringify(viewInput(view)));
      setValue(viewInput(view));
      queryClient.setQueryData(["work-view", view.id], view);
      await queryClient.invalidateQueries({ queryKey: ["work-views"] });
      if (!saved) navigate(`/views/${view.id}`, { replace: true });
    },
  });
  const taskId = searchParams.get("task");
  const openTask = (id: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (id) next.set("task", id);
    else next.delete("task");
    setSearchParams(next);
  };
  const openRecord = (record: ViewRecord) => {
    if (value.entity_type === "tasks") openTask(record.id);
    else if (value.entity_type === "sessions") navigate(`/c/${record.id}`);
    else navigate(`/views/new?type=tasks&project=${record.id}`);
  };
  return (
    <>
      <header className="flex items-center gap-2 border-b border-border/60 px-5 py-4 text-sm">
        <Link to="/views" className="text-muted-foreground hover:text-foreground">
          Views
        </Link>
        <ChevronRightIcon className="size-4 text-muted-foreground" />
        <span className="truncate">{saved?.name ?? "New view"}</span>
      </header>
      <div className="m-4 rounded-xl border border-border/70 bg-background shadow-sm">
        <div className="flex flex-wrap items-start gap-3 px-5 pt-5 pb-4">
          <LayersIcon className="mt-2 size-5 shrink-0 text-muted-foreground" />
          <div className="min-w-40 flex-1 space-y-1">
            <Input
              aria-label="View name"
              value={value.name}
              maxLength={120}
              onChange={(event) => setValue({ ...value, name: event.target.value })}
              placeholder="Name your view"
              className="h-10 border-transparent px-1 text-lg font-medium shadow-none"
            />
            <Input
              aria-label="View description"
              value={value.description}
              maxLength={1000}
              onChange={(event) => setValue({ ...value, description: event.target.value })}
              placeholder="Description (optional)"
              className="border-transparent px-1 text-sm text-muted-foreground shadow-none"
            />
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="mr-2 hidden items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-xs text-muted-foreground sm:flex">
              <LockKeyholeIcon className="size-3" />
              Personal
            </span>
            <Button asChild variant="ghost">
              <Link to="/views">Cancel</Link>
            </Button>
            <Button
              disabled={!value.name.trim() || save.isPending || baseline === JSON.stringify(value)}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : saved ? "Save changes" : "Create view"}
            </Button>
          </div>
        </div>
        <ViewControls
          value={value}
          projects={projectOptions}
          onChange={setValue}
          showEntityTabs={!saved}
        />
        {save.error && (
          <p role="alert" className="px-5 py-3 text-sm text-destructive">
            {save.error.message}
          </p>
        )}
      </div>
      <div className="px-4 pb-4">
        {projects.error && (
          <p role="alert" className="mb-3 text-sm text-destructive">
            Couldn’t load projects. {projects.error.message}
          </p>
        )}
        <QueriedViewResults
          definition={value}
          onOpen={openRecord}
          notice={saved && baseline !== JSON.stringify(value) ? "Unsaved changes" : undefined}
        />
      </div>
      {taskId && taskId !== "new" && (
        <WorkTaskDialog
          taskId={taskId}
          projects={projectOptions}
          onClose={() => openTask(null)}
          onSaved={openTask}
        />
      )}
    </>
  );
}
