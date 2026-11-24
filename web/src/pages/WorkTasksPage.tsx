import { useEffect, useState } from "react";
import { PlusIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useProjects } from "@/hooks/useConversations";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { newView } from "@/lib/workViewsApi";
import { QueriedViewResults } from "@/views/QueriedViewResults";
import { ViewControls } from "@/views/ViewControls";
import { CreateWorkTaskDialog } from "@/views/WorkTaskEditor";

export function WorkTasksPage() {
  const info = useServerInfo();
  if (info === "loading")
    return (
      <PageScroll>
        <Spinner aria-label="Loading tasks" />
      </PageScroll>
    );
  if (!info.work_views)
    return (
      <PageScroll>
        <p>Tasks are unavailable on this server.</p>
      </PageScroll>
    );
  return <TaskList />;
}

function TaskList() {
  const [value, setValue] = useState(() => newView("tasks"));
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const projects = useProjects();
  const projectOptions = (projects.data ?? []).flatMap((project) =>
    project.id ? [{ id: project.id, name: project.name }] : [],
  );
  const taskId = searchParams.get("task");
  useEffect(() => {
    if (taskId && taskId !== "new")
      navigate(`/work-tasks/${encodeURIComponent(taskId)}`, { replace: true });
  }, [taskId, navigate]);
  const openTask = (id: string) => {
    if (id === "new") {
      const next = new URLSearchParams(searchParams);
      next.set("task", "new");
      setSearchParams(next);
    } else navigate(`/work-tasks/${id}`);
  };
  return (
    <PageScroll maxWidthClassName="max-w-none" extraTop="0.5rem" contentClassName="px-3 sm:px-6">
      <div className="min-h-[70vh] rounded-2xl border border-border/60 bg-background/80">
        <header className="flex items-center justify-between border-b border-border/60 px-5 py-4">
          <h1 className="text-lg font-semibold">Tasks</h1>
          <Button onClick={() => openTask("new")}>
            <PlusIcon />
            New task
          </Button>
        </header>
        <ViewControls
          value={value}
          projects={projectOptions}
          onChange={setValue}
          showEntityTabs={false}
        />
        <div className="px-4 pb-4">
          {projects.error && (
            <p role="alert" className="mt-3 text-sm text-destructive">
              Couldn’t load projects. {projects.error.message}
            </p>
          )}
          <QueriedViewResults
            definition={value}
            onOpen={(record) => openTask(record.id)}
            emptyMessage="Create a task or adjust your filters."
          />
        </div>
      </div>
      {taskId === "new" && (
        <CreateWorkTaskDialog
          projects={projectOptions}
          initialProjectId={
            value.filters.project_ids.length === 1 ? value.filters.project_ids[0] : undefined
          }
          onClose={() => {
            const next = new URLSearchParams(searchParams);
            next.delete("task");
            setSearchParams(next);
          }}
          onCreated={(id) => navigate(`/work-tasks/${id}`, { replace: true })}
        />
      )}
    </PageScroll>
  );
}
