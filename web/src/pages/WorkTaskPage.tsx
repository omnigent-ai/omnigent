import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRightIcon, PencilIcon, Trash2Icon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
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
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { deleteWorkTask, getWorkTask } from "@/lib/workViewsApi";
import { PRIORITIES, statusLabel } from "@/views/viewPresentation";
import { StatusIcon } from "@/views/ViewResults";
import { TaskSessions, WorkTaskForm } from "@/views/WorkTaskEditor";

export function WorkTaskPage() {
  const info = useServerInfo();
  const { taskId = "" } = useParams<{ taskId: string }>();
  return (
    <PageScroll maxWidthClassName="max-w-4xl">
      {info === "loading" ? (
        <Spinner aria-label="Loading task" />
      ) : !info.work_views ? (
        <p>Tasks are unavailable on this server.</p>
      ) : (
        <TaskDetail key={taskId} taskId={taskId} />
      )}
    </PageScroll>
  );
}

function TaskDetail({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const projects = useProjects();
  const projectOptions = (projects.data ?? []).flatMap((project) =>
    project.id ? [{ id: project.id, name: project.name }] : [],
  );
  const query = useQuery({
    queryKey: ["work-task", taskId],
    queryFn: () => getWorkTask(taskId),
    refetchOnMount: "always",
  });
  const task = query.data;
  const remove = useMutation({
    mutationFn: () => deleteWorkTask(taskId),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: ["work-task", taskId], exact: true });
      void queryClient.invalidateQueries({ queryKey: ["view-results", "tasks"] });
      navigate("/work-tasks", { replace: true });
    },
  });
  return (
    <div className="space-y-8 pb-8">
      <nav aria-label="Task breadcrumb" className="flex items-center gap-2 text-sm">
        <Link to="/work-tasks" className="text-muted-foreground hover:text-foreground">
          Tasks
        </Link>
        <ChevronRightIcon className="size-4 text-muted-foreground" />
        <span className="truncate">{task?.title ?? "Task"}</span>
      </nav>
      {query.isPending || !query.isFetchedAfterMount ? (
        <Spinner aria-label="Loading task" />
      ) : (
        <>
          {query.error && (
            <p role="alert" className="text-sm text-destructive">
              {task ? "Couldn’t refresh this task. " : "Couldn’t load this task. "}
              {query.error.message}
              <Button variant="ghost" onClick={() => void query.refetch()}>
                Retry
              </Button>
            </p>
          )}
          {task && (
            <>
              <header className="flex flex-col items-start justify-between gap-4 sm:flex-row">
                <h1 className="min-w-0 break-words text-2xl font-semibold">{task.title}</h1>
                {!editing && (
                  <div className="flex shrink-0 items-center gap-2">
                    <Button variant="outline" onClick={() => setEditing(true)}>
                      <PencilIcon />
                      Edit task
                    </Button>
                    <Button
                      variant="outline"
                      className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                      onClick={() => setDeleting(true)}
                    >
                      <Trash2Icon />
                      Delete task
                    </Button>
                  </div>
                )}
              </header>
              {editing ? (
                <WorkTaskForm
                  task={task}
                  projects={projectOptions}
                  onClose={() => setEditing(false)}
                  onSaved={() => setEditing(false)}
                />
              ) : (
                <>
                  <dl className="flex flex-wrap gap-x-10 gap-y-4 text-sm">
                    <div className="space-y-2">
                      <dt className="text-muted-foreground">Status</dt>
                      <dd className="flex items-center gap-2">
                        <StatusIcon status={task.status} />
                        {statusLabel(task.status)}
                      </dd>
                    </div>
                    <div className="space-y-2">
                      <dt className="text-muted-foreground">Priority</dt>
                      <dd>{PRIORITIES[task.priority]}</dd>
                    </div>
                    <div className="space-y-2">
                      <dt className="text-muted-foreground">Project</dt>
                      <dd>{task.project_name ?? "No project"}</dd>
                    </div>
                  </dl>
                  <section aria-label="Description" className="space-y-3">
                    <h2 className="text-sm font-medium">Description</h2>
                    <p className="whitespace-pre-wrap break-words text-sm leading-6 text-muted-foreground">
                      {task.description || "No description yet."}
                    </p>
                  </section>
                  <TaskSessions
                    task={task}
                    onStart={() => {
                      const prompt = [task.title, task.description].filter(Boolean).join("\n\n");
                      const location = newSessionLocation(prompt, []);
                      const context = new URLSearchParams({ work_task: task.id });
                      if (task.project_name) context.set("project", task.project_name);
                      navigate(`${location}&${context}`);
                    }}
                  />
                </>
              )}
              {projects.error && (
                <p role="alert" className="text-sm text-destructive">
                  Couldn’t load projects. {projects.error.message}
                </p>
              )}
              <Dialog
                open={deleting}
                onOpenChange={(open) => {
                  if (remove.isPending) return;
                  setDeleting(open);
                  if (!open) remove.reset();
                }}
              >
                <DialogContent showCloseButton={!remove.isPending}>
                  <DialogHeader>
                    <DialogTitle>Delete task?</DialogTitle>
                    <DialogDescription className="break-words">
                      “{task.title}” will be permanently deleted. Linked sessions will be kept and
                      unlinked from this task. This cannot be undone.
                    </DialogDescription>
                  </DialogHeader>
                  {remove.error && (
                    <p role="alert" className="text-sm text-destructive">
                      Couldn’t delete this task. {remove.error.message}
                    </p>
                  )}
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="ghost"
                      disabled={remove.isPending}
                      onClick={() => {
                        setDeleting(false);
                        remove.reset();
                      }}
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="destructive"
                      disabled={remove.isPending}
                      onClick={() => remove.mutate()}
                    >
                      {remove.isPending ? "Deleting…" : "Delete task"}
                    </Button>
                  </div>
                </DialogContent>
              </Dialog>
            </>
          )}
        </>
      )}
    </div>
  );
}
