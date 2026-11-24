import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LinkIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/ui/spinner";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import type { Project } from "@/lib/projectsApi";
import { Link } from "@/lib/routing";
import {
  getWorkTask,
  linkWorkSession,
  newView,
  queryWorkView,
  saveWorkTask,
  unlinkWorkSession,
  type WorkTask,
  type WorkTaskInput,
} from "@/lib/workViewsApi";
import { ViewSelect } from "./ViewControls";
import { PRIORITIES, TASK_STATUSES } from "./viewPresentation";
import { StatusIcon } from "./ViewResults";

export function WorkTaskDialog({
  taskId,
  projects,
  initialProjectId,
  onClose,
  onSaved,
}: {
  taskId: string;
  projects: Pick<Project, "id" | "name">[];
  initialProjectId?: string;
  onClose: () => void;
  onSaved: (id: string) => void;
}) {
  const task = useQuery({
    queryKey: ["work-task", taskId],
    queryFn: () => getWorkTask(taskId),
    enabled: taskId !== "new",
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{taskId === "new" ? "New task" : "Task"}</DialogTitle>
          <DialogDescription>
            {taskId === "new"
              ? "Track work on its own or in a project."
              : "Keep the outcome and its agent sessions together."}
          </DialogDescription>
        </DialogHeader>
        {taskId !== "new" && task.isPending ? (
          <Spinner aria-label="Loading task" />
        ) : task.error ? (
          <div role="alert">
            {task.error.message}
            <Button variant="ghost" onClick={() => void task.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <TaskForm
            key={taskId}
            task={task.data}
            projects={projects}
            initialProjectId={initialProjectId}
            onClose={onClose}
            onSaved={onSaved}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function TaskForm({
  task,
  projects,
  initialProjectId,
  onClose,
  onSaved,
}: {
  task?: WorkTask;
  projects: Pick<Project, "id" | "name">[];
  initialProjectId?: string;
  onClose: () => void;
  onSaved: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [body, setBody] = useState<WorkTaskInput>(() => ({
    title: task?.title ?? "",
    description: task?.description ?? "",
    status: task?.status ?? "todo",
    priority: task?.priority ?? 0,
    project_id: task ? task.project_id : (initialProjectId ?? null),
  }));
  const update = (changes: Partial<WorkTaskInput>) =>
    setBody((previous) => ({ ...previous, ...changes }));
  const save = useMutation({
    mutationFn: () => saveWorkTask(body, task?.id),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: ["view-results"] });
      await queryClient.invalidateQueries({ queryKey: ["work-task", saved.id] });
      onSaved(saved.id);
    },
  });
  return (
    <div className="space-y-5">
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (body.title.trim() && !save.isPending) save.mutate();
        }}
      >
        <Input
          autoFocus
          aria-label="Task title"
          placeholder="Task title"
          maxLength={200}
          value={body.title}
          onChange={(event) => update({ title: event.target.value })}
          className="h-11 text-lg font-medium"
        />
        <Textarea
          aria-label="Task description"
          placeholder="Describe the outcome and any useful context…"
          maxLength={12000}
          value={body.description}
          onChange={(event) => update({ description: event.target.value })}
          className="min-h-32"
        />
        <div className="flex flex-wrap gap-3">
          <ViewSelect
            label="Task status"
            value={body.status}
            options={TASK_STATUSES}
            onChange={(status) => update({ status: status as WorkTaskInput["status"] })}
          />
          <ViewSelect
            label="Task priority"
            value={String(body.priority)}
            options={PRIORITIES}
            onChange={(priority) => update({ priority: Number(priority) })}
          />
          <ViewSelect
            label="Task project"
            value={body.project_id ?? "none"}
            options={{
              none: "No project",
              ...Object.fromEntries(projects.map((project) => [project.id, project.name])),
            }}
            onChange={(project_id) =>
              update({ project_id: project_id === "none" ? null : project_id })
            }
          />
        </div>
        {save.error && (
          <p role="alert" className="text-sm text-destructive">
            {save.error.message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={!body.title.trim() || save.isPending}>
            {save.isPending ? "Saving…" : task ? "Save task" : "Create task"}
          </Button>
        </div>
      </form>
      {task && <TaskSessions task={task} />}
    </div>
  );
}

function TaskSessions({ task }: { task: WorkTask }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [search, setSearch] = useState("");
  const [involvement, setInvolvement] = useState("");
  const settledSearch = useDebouncedValue(search, 200);
  const definition = newView("sessions");
  definition.filters.search = settledSearch;
  definition.filters.include_archived = true;
  const candidates = useInfiniteQuery({
    queryKey: ["task-session-candidates", settledSearch],
    queryFn: ({ pageParam, signal }) => queryWorkView(definition, pageParam, signal, 30),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: adding,
  });
  const change = useMutation({
    mutationFn: ({ id, remove }: { id: string; remove?: boolean }) =>
      remove ? unlinkWorkSession(task.id, id) : linkWorkSession(task.id, id, involvement),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["work-task", task.id] });
      await queryClient.invalidateQueries({ queryKey: ["view-results"] });
      setAdding(false);
      setSearch("");
      setInvolvement("");
    },
  });
  const linked = new Set(task.sessions.map((session) => session.session_id));
  return (
    <section className="space-y-3 border-t border-border pt-4" aria-label="Linked sessions">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">
          Sessions <span className="text-muted-foreground">{task.sessions.length}</span>
        </h3>
        <Button size="sm" variant="ghost" onClick={() => setAdding(!adding)}>
          <PlusIcon />
          Link session
        </Button>
      </div>
      {task.sessions.length === 0 && (
        <p className="text-sm text-muted-foreground">No sessions linked yet.</p>
      )}
      {task.sessions.map((session) => (
        <div
          key={session.session_id}
          className="flex items-center gap-2 rounded-lg border border-border/60 p-3"
        >
          <StatusIcon status={session.status} />
          <div className="min-w-0 flex-1">
            <Link
              to={`/c/${session.session_id}`}
              className="block truncate text-sm font-medium hover:underline"
            >
              {session.title}
            </Link>
            {session.involvement && (
              <p className="truncate text-xs text-muted-foreground">{session.involvement}</p>
            )}
          </div>
          <Button
            size="icon-sm"
            variant="ghost"
            disabled={change.isPending}
            aria-label={`Unlink ${session.title}`}
            onClick={() => change.mutate({ id: session.session_id, remove: true })}
          >
            <XIcon />
          </Button>
        </div>
      ))}
      {change.error && (
        <p role="alert" className="text-sm text-destructive">
          {change.error.message}
        </p>
      )}
      {adding && (
        <div className="space-y-2 rounded-lg border border-border p-3">
          <Input
            aria-label="Search sessions to link"
            placeholder="Search sessions…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Input
            aria-label="Session contribution"
            placeholder="Contribution (optional), e.g. implementation"
            maxLength={300}
            value={involvement}
            onChange={(event) => setInvolvement(event.target.value)}
          />
          <div className="max-h-48 space-y-1 overflow-y-auto">
            {candidates.isPending ? (
              <Spinner aria-label="Loading sessions" />
            ) : candidates.error ? (
              <p role="alert" className="text-sm text-destructive">
                {candidates.error.message}
              </p>
            ) : (
              candidates.data?.pages
                .flatMap((page) => page.data)
                .filter((record) => !linked.has(record.id))
                .map((record) => (
                  <Button
                    key={record.id}
                    variant="ghost"
                    className="w-full justify-start"
                    disabled={change.isPending}
                    onClick={() => change.mutate({ id: record.id })}
                  >
                    <LinkIcon className="shrink-0" />
                    <span className="truncate">{record.title}</span>
                  </Button>
                ))
            )}
            {candidates.data &&
              candidates.data.pages.every((page) =>
                page.data.every((record) => linked.has(record.id)),
              ) && <p className="p-2 text-sm text-muted-foreground">No matching sessions.</p>}
          </div>
          {candidates.hasNextPage && (
            <Button
              variant="ghost"
              size="sm"
              disabled={candidates.isFetchingNextPage}
              onClick={() => void candidates.fetchNextPage()}
            >
              Load more sessions
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
