/**
 * Jobs page (`/jobs`).
 *
 * Lists the jobs the user has created — each job is a named, saved flow chart
 * (see {@link jobsStore}). "Create flow" makes a new empty job and opens it in
 * the flow builder (`/jobs/flow/:jobId`); clicking a job reopens its chart.
 * Jobs can be renamed or deleted inline. Persistence is browser localStorage;
 * there is no server involvement.
 */

import { useState } from "react";
import { PencilIcon, PlayIcon, PlusIcon, Trash2Icon, WorkflowIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { relativeTime } from "@/lib/relativeTime";
import { Link, useNavigate } from "@/lib/routing";
import { createJob, deleteJob, runJob, updateJob, useJobs } from "@/lib/jobsStore";

export function JobsPage() {
  const jobs = useJobs();
  const navigate = useNavigate();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [runningId, setRunningId] = useState<string | null>(null);

  const onCreate = async () => {
    const job = await createJob("Untitled flow");
    navigate(`/jobs/flow/${job.id}`);
  };

  const onRun = async (id: string) => {
    setRunningId(id);
    try {
      const run = await runJob(id);
      if (run.sessionId) navigate(`/c/${run.sessionId}`);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Failed to run job");
    } finally {
      setRunningId(null);
    }
  };

  const startRename = (id: string, current: string) => {
    setRenamingId(id);
    setRenameValue(current);
  };
  const commitRename = (id: string) => {
    const v = renameValue.trim();
    if (v) void updateJob(id, { name: v });
    setRenamingId(null);
  };

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Jobs</h1>
        <Button onClick={onCreate}>
          <PlusIcon className="size-4" /> Create flow
        </Button>
      </div>

      {jobs.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <WorkflowIcon className="size-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">No jobs yet</p>
          <p className="text-xs text-muted-foreground">
            Create a flow to design a chart and generate its narrative, outline, and Mermaid.
          </p>
          <Button className="mt-2" variant="outline" onClick={onCreate}>
            <PlusIcon className="size-4" /> Create flow
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {jobs.map((job) => {
            const nodeCount = job.graph.nodes?.length ?? 0;
            const isRenaming = renamingId === job.id;
            return (
              <div
                key={job.id}
                data-testid="job-row"
                className="flex items-center gap-3 rounded-xl border border-border bg-card p-4"
              >
                <WorkflowIcon className="size-5 shrink-0 text-muted-foreground" />
                <div className="flex min-w-0 flex-1 flex-col">
                  {isRenaming ? (
                    <input
                      autoFocus
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onBlur={() => commitRename(job.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitRename(job.id);
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                      className="w-full max-w-sm rounded-md border border-input bg-background px-2 py-1 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                  ) : (
                    <Link
                      to={`/jobs/flow/${job.id}`}
                      className="truncate text-sm font-medium hover:underline"
                    >
                      {job.name}
                    </Link>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {nodeCount === 1 ? "1 node" : `${nodeCount} nodes`} · updated{" "}
                    {relativeTime(job.updatedAt)}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Run job now"
                    disabled={runningId === job.id}
                    onClick={() => onRun(job.id)}
                  >
                    <PlayIcon className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Rename job"
                    onClick={() => startRename(job.id, job.name)}
                  >
                    <PencilIcon className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Delete job"
                    onClick={() => {
                      if (window.confirm(`Delete “${job.name}”?`)) void deleteJob(job.id);
                    }}
                  >
                    <Trash2Icon className="size-3.5" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </PageScroll>
  );
}
