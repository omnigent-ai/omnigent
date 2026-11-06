import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useProjects } from "@/hooks/useConversations";
import { useCreateScheduledTask } from "@/hooks/useScheduledTasks";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isBootFallbackInfo } from "@/lib/bootCapabilities";
import { pickerCuration } from "@/lib/capabilities";
import { selectableSessionAgents } from "@/lib/agentGrouping";
import { nativeDisplayNameForAgent } from "@/lib/nativeCodingAgents";
import { describeSchedule } from "@/lib/scheduleText";
import {
  getSharedScheduledTask,
  listSharedScheduledTasks,
  scheduledTaskCopyInput,
  ScheduledTaskApiError,
  type ScheduledTask,
} from "@/lib/scheduledTasksApi";
import { CreateScheduledTaskDialog } from "./CreateScheduledTaskDialog";
import { prepareScheduledTaskCopy, scheduledTaskCopyAgents } from "./copyScheduledTask";
import { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";

export function SharedAutomations({
  sharedTaskId,
  onCopied,
}: {
  sharedTaskId: string | null;
  onCopied: () => void;
}) {
  const shared = useQuery({
    queryKey: ["shared-scheduled-tasks"],
    queryFn: listSharedScheduledTasks,
    refetchOnWindowFocus: true,
  });
  const [copying, setCopying] = useState<ScheduledTask | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function copy(taskId: string) {
    setError(null);
    setLoadingId(taskId);
    try {
      setCopying(await getSharedScheduledTask(taskId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load this automation.");
    } finally {
      setLoadingId(null);
    }
  }
  return (
    <div className="space-y-3">
      <p className="text-ui text-muted-foreground">
        View automations shared with you, or copy one to make it your own. Copies run using your
        settings and credentials.
      </p>
      {shared.isLoading && <p>Loading shared automations…</p>}
      {shared.error && (
        <div role="alert">
          Could not load shared automations.{" "}
          <Button variant="outline" size="sm" onClick={() => void shared.refetch()}>
            Retry
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error}
        </p>
      )}
      {shared.data?.length === 0 && (
        <p className="py-8 text-muted-foreground">No automations have been shared with you yet.</p>
      )}
      {sharedTaskId && shared.data && !shared.data.some((task) => task.id === sharedTaskId) && (
        <p role="alert">This automation is no longer shared with you or has been deleted.</p>
      )}
      {shared.data?.map((task) => (
        <div key={task.id} className="rounded-xl border border-border bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="font-semibold break-words">{task.name}</h2>
              <p className="text-ui text-muted-foreground">Shared by {task.ownerUserId}</p>
              <p className="text-ui text-muted-foreground">
                {describeSchedule(task.rrule)} · {task.timezone}
              </p>
            </div>
            <Button
              size="sm"
              disabled={loadingId !== null || copying !== null}
              onClick={() => void copy(task.id)}
            >
              {loadingId === task.id || copying?.id === task.id ? "Copying…" : "Copy"}
            </Button>
          </div>
          <details className="mt-3 text-ui" open={sharedTaskId === task.id || undefined}>
            <summary className="cursor-pointer">View configuration</summary>
            <p className="my-3 whitespace-pre-wrap break-words">{task.prompt}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 break-all text-muted-foreground">
              <dt>Agent</dt>
              <dd>
                {task.agentName
                  ? nativeDisplayNameForAgent({ name: task.agentName, harness: null })
                  : "Choose when copying"}
              </dd>
              {task.sharedAgent && (
                <>
                  <dt>Custom agent</dt>
                  <dd>A separate copy will be added to your agents.</dd>
                </>
              )}
              <dt>Run on</dt>
              <dd>
                {task.executionTarget === "managed_sandbox"
                  ? "Sandbox"
                  : (task.hostName ??
                    (task.hostId ? "Connected host" : "Most recently active host"))}
              </dd>
              <dt>Workspace</dt>
              <dd>{task.workspace ?? "Default"}</dd>
              <dt>Sessions</dt>
              <dd>
                {task.sessionMode === "continue"
                  ? "Continue in one session"
                  : "New session per run"}
              </dd>
              <dt>Model / effort</dt>
              <dd>
                {task.modelOverride ?? "Default"} / {task.reasoningEffort ?? "Default"}
              </dd>
              <dt>Permissions</dt>
              <dd>{task.permissionMode ?? "Default"}</dd>
              <dt>Budget per run</dt>
              <dd>{task.maxCostUsd == null ? "Default" : `$${task.maxCostUsd}`}</dd>
              <dt>Project</dt>
              <dd>
                {task.projectName ?? (task.projectId ? "Choose your project when copying" : "None")}
              </dd>
              {task.baseBranch && (
                <>
                  <dt>Worktree base</dt>
                  <dd>{task.baseBranch}</dd>
                </>
              )}
            </dl>
          </details>
        </div>
      ))}
      {copying && (
        <CopySharedAutomation
          key={copying.id}
          source={copying}
          onClose={() => setCopying(null)}
          onCopied={onCopied}
        />
      )}
    </div>
  );
}

function CopySharedAutomation({
  source,
  onClose,
  onCopied,
}: {
  source: ScheduledTask;
  onClose: () => void;
  onCopied: () => void;
}) {
  const info = useServerInfo();
  const agents = useAvailableAgents();
  const hosts = useHosts();
  const projectsSupported = info !== "loading" && info.scheduled_task_projects === true;
  const projects = useProjects({ enabled: projectsSupported });
  const create = useCreateScheduledTask();
  const [editor, setEditor] = useState<{ draft: ScheduledTask; issues: string[] } | null>(null);
  const started = useRef(false);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const copyAgents = scheduledTaskCopyAgents(source, agents.data ?? []);
  const agent = copyAgents.find((item) => item.id === source.agentId);
  const host = hosts.data?.find((item) => item.host_id === source.hostId);
  const settings = useScheduledHarnessSettings(
    agent,
    host,
    source.executionTarget === "managed_sandbox",
    source.modelOverride ?? "",
    !!agent,
    source.sharedAgent?.id === agent?.id,
  );
  useEffect(() => {
    if (started.current || info === "loading" || isBootFallbackInfo(info)) return;
    const loadError = agents.error ?? hosts.error ?? (projectsSupported ? projects.error : null);
    if (loadError) {
      started.current = true;
      showToast("Could not check your automation settings. Please try copying again.");
      onClose();
      return;
    }
    if (!agents.data || !hosts.data || (projectsSupported && !projects.data) || settings.loading)
      return;
    started.current = true;
    const allowed = selectableSessionAgents(copyAgents, pickerCuration(info).restrictToAgentNames);
    const prepared = prepareScheduledTaskCopy(
      source,
      allowed,
      hosts.data,
      projects.data ?? [],
      info.managed_sandboxes_enabled,
    );
    if (settings.blockedReason) prepared.issues.push(`Harness: ${settings.blockedReason}`);
    if (
      source.modelOverride &&
      (!settings.supportsModel ||
        (settings.models.length > 0 &&
          !settings.models.some((model) => model.id === source.modelOverride)))
    ) {
      prepared.issues.push("Model: choose one available for your execution target.");
      prepared.draft.modelOverride = null;
      prepared.draft.reasoningEffort = null;
    }
    if (
      source.reasoningEffort &&
      !settings.efforts.some((effort) => effort.value === source.reasoningEffort)
    ) {
      prepared.issues.push("Effort: choose a supported value or Default.");
      prepared.draft.reasoningEffort = null;
    }
    if (
      source.permissionMode &&
      !settings.permissions.some((permission) => permission.value === source.permissionMode)
    ) {
      prepared.issues.push("Permissions: choose a supported mode or Default.");
      prepared.draft.permissionMode = null;
    }
    if (prepared.issues.length) {
      setEditor(prepared);
      return;
    }
    void create.mutateAsync(scheduledTaskCopyInput(prepared.draft)).then(
      () => {
        if (!mounted.current) return;
        showToast("Automation copied to Your automations");
        onCopied();
      },
      (err: unknown) => {
        if (!mounted.current) return;
        if (err instanceof ScheduledTaskApiError && [400, 403, 404, 422].includes(err.status)) {
          setEditor({ draft: prepared.draft, issues: [err.message] });
        } else {
          showToast(err instanceof Error ? err.message : "Could not copy this automation.");
          onClose();
        }
      },
    );
  }, [
    info,
    agents,
    copyAgents,
    hosts,
    projects,
    projectsSupported,
    settings,
    source,
    create,
    onClose,
    onCopied,
  ]);
  return editor ? (
    <CreateScheduledTaskDialog
      open
      initialTask={editor.draft}
      allowUnpinnedHost={source.executionTarget === "connected_host" && source.hostId === null}
      copyIssues={editor.issues}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      onCreated={() => {
        showToast("Automation copied to Your automations");
        onCopied();
      }}
    />
  ) : null;
}
