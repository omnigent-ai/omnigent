// Dialog for creating or editing a scheduled task. Reuses the existing agent,
// host, and workspace pickers where the backend can persist those fields.

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDownIcon, FolderOpenIcon, TriangleAlertIcon } from "lucide-react";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/scheduled/Label";
import { ScheduleFields } from "@/components/scheduled/ScheduleFields";
import { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";
import { ModelEffortFields } from "@/components/scheduled/ModelEffortFields";
import { TriggerTemplateFields } from "@/components/scheduled/TriggerTemplateFields";
import {
  emptyTriggerFields,
  renderTriggerPrompt,
  TRIGGER_TEMPLATES,
  triggerFieldsComplete,
  type TriggerFields,
  type TriggerSource,
} from "@/components/scheduled/triggerTemplates";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { ProjectPicker, ProjectRowIcon } from "@/shell/ProjectPicker";
import { useProjects, useResolveOrCreateProjectId } from "@/hooks/useConversations";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, sandboxOptionLabel } from "@/lib/capabilities";
import { useCreateScheduledTask, useUpdateScheduledTask } from "@/hooks/useScheduledTasks";
import { isNativeCodingAgent } from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent, selectableSessionAgents } from "@/lib/agentGrouping";
import {
  isBackdropOverlay,
  isInsidePopper,
  shouldGuardDialogDismiss,
} from "@/lib/dialogDismissGuard";
import {
  buildRRule,
  DEFAULT_SCHEDULE_MODEL,
  parseRRuleToScheduleModel,
  validateSchedule,
  type ScheduleModel,
} from "@/lib/scheduleBuilder";
import {
  ScheduledTaskApiError,
  type ScheduledTask,
  type AutomationTemplateId,
  type ScheduledTaskExecutionTarget,
  type ScheduledTaskSessionMode,
} from "@/lib/scheduledTasksApi";
import { isValidSandboxRepoUrl, splitSandboxWorkspace } from "@/lib/sandboxRepositories";
import { localTimezone } from "@/lib/timezones";

const PROVIDER_DEFAULT = "provider-default";

export function CreateScheduledTaskDialog({
  open,
  onOpenChange,
  initialName,
  initialPrompt,
  initialTrigger = null,
  initialTemplateId,
  sessionLink = "",
  editingTask = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Seed values applied when opening from a template card. */
  initialName?: string;
  initialPrompt?: string;
  /** Open on a Jira/Slack/GitHub trigger template instead of a plain prompt. */
  initialTrigger?: TriggerSource | null;
  initialTemplateId?: AutomationTemplateId;
  /** Shareable session URL the trigger prompt links back with; see `SESSION_LINK_ID_PLACEHOLDER`. */
  sessionLink?: string;
  editingTask?: ScheduledTask | null;
}) {
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts } = useHosts({ enabled: open });
  const info = useServerInfo();
  const anchoredScheduling = isFeatureEnabled(info, "automations_v2");
  // Gates the "new sandbox each run" option: only servers that can actually
  // serve a managed launch advertise it (same flag New Chat's sandbox option
  // gates on). "loading" → treat as disabled until /v1/info resolves.
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  // Older servers reject a task's project fields, so the picker needs the flag.
  const projectsSupported = info !== "loading" && info.scheduled_task_projects === true;
  const sandboxRepoCatalog =
    info !== "loading" && info.scheduled_task_sandbox_workspaces === true && info.sandbox_provider
      ? (info.sandbox_provider_capabilities?.[info.sandbox_provider]?.repo_catalog ?? [])
      : [];

  const {
    data: projects = [],
    isSuccess: projectsLoaded,
    isError: projectsFailed,
  } = useProjects({ enabled: open && projectsSupported });
  const resolveProjectId = useResolveOrCreateProjectId();
  const sessionModesSupported = info !== "loading" && info.scheduled_task_session_modes === true;
  // Provider-named label for the sandbox host option (e.g. "Modal Sandbox"),
  // falling back to the generic "New Sandbox" — same helper the New Chat picker uses.
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  const createMutation = useCreateScheduledTask();
  const updateMutation = useUpdateScheduledTask();
  const isEdit = editingTask !== null;

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [schedule, setSchedule] = useState<ScheduleModel>(DEFAULT_SCHEDULE_MODEL);
  const [timezone, setTimezone] = useState(localTimezone);

  // Empty overrides use the agent's configured defaults when a run starts.
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(true);
  const [pickedModel, setPickedModel] = useState<string>("");
  const [pickedEffort, setPickedEffort] = useState<string>("");
  const [pickedPermission, setPickedPermission] = useState<string>("");

  const agentList = useMemo(() => selectableSessionAgents(agents ?? []), [agents]);
  const harnessEntries = useMemo(
    () => agentList.filter((a) => isNativeCodingAgent(a) || isAcpHarnessAgent(a)),
    [agentList],
  );
  const agentEntries = useMemo(
    () => agentList.filter((a) => !isNativeCodingAgent(a) && !isAcpHarnessAgent(a)),
    [agentList],
  );
  // Resolve the effective selection: the explicit pick if it's still in the
  // list, else the edited task's own agent (which may be hidden from the picker
  // — never silently retarget it), else the first agent (so a fresh picker
  // always has a concrete value).
  const effectiveAgentId =
    (agentList.some((a) => a.id === pickedAgentId)
      ? pickedAgentId
      : isEdit
        ? editingTask?.agentId
        : agentList[0]?.id) ?? null;
  const selectedAgent = agentList.find((a) => a.id === effectiveAgentId);
  const agentLabel = selectedAgent
    ? selectedAgent.display_name
    : isEdit && editingTask
      ? editingTask.agentId
      : "Select agent";
  // Editing a task only rebinds the agent when the user actually picks a
  // different one — the prefill starts equal to the task's own agent.
  const agentChanged =
    isEdit && effectiveAgentId !== null && effectiveAgentId !== editingTask?.agentId;

  function handleSelectAgent(agent: AvailableAgent) {
    setPickedAgentId(agent.id);
    setAdvancedOpen(true);
    // Settings belong to their harness. Returning to the edited task's agent
    // restores its saved values; selecting another agent clears them.
    const backToOriginal = isEdit && agent.id === editingTask?.agentId;
    setPickedModel(
      backToOriginal && originalTargetSelected ? (editingTask?.modelOverride ?? "") : "",
    );
    setPickedEffort(
      backToOriginal && originalTargetSelected ? (editingTask?.reasoningEffort ?? "") : "",
    );
    setPickedPermission(backToOriginal ? (editingTask?.permissionMode ?? "") : "");
  }

  // ── Nested dropdown dismiss guard ─────────────────────────────────────────
  // The agent picker and host/schedule Selects portal dropdowns OUTSIDE DialogContent.
  // Two dismiss paths leak through to the Dialog and close the whole modal:
  //   (a) picking an option — the closing pointerdown lands in the popper;
  //   (b) clicking empty modal body (or the trigger) while a dropdown is open —
  //       the target is the dialog body, and the portal ALSO emits a
  //       focus-outside as it unmounts.
  // Target-sniffing (isInsidePopper) only covers (a). To cover (b) too, track
  // whether ANY dropdown is open, and keep the guard armed for a short grace
  // window after it closes so the trailing pointerup/focus transition that
  // Radix reports as "interact outside" is absorbed. See `guardDialogDismiss`.
  const selectOpenCountRef = useRef(0);
  const selectClosedAtRef = useRef(0);
  const [workspaceBrowserOpen, setWorkspaceBrowserOpen] = useState(false);
  function handleSelectOpenChange(isOpen: boolean) {
    if (isOpen) {
      selectOpenCountRef.current += 1;
    } else {
      selectOpenCountRef.current = Math.max(0, selectOpenCountRef.current - 1);
      selectClosedAtRef.current = Date.now();
    }
  }
  /** preventDefault the Dialog's outside-dismiss ONLY for the narrow nested-Select
   * cases — a click inside portalled dropdown content (path a) or while a dropdown
   * is open (path b),
   * plus a short grace window for the trailing focus-outside a dropdown emits as
   * it unmounts. A genuine click on the backdrop OVERLAY always dismisses: its target
   * is the overlay itself (never a popper), so we let it through even inside the
   * grace window — this is the fix for backdrop-click-to-close being swallowed.
   * Escape + Cancel are unaffected (they don't route through this guard). */
  function guardDialogDismiss(event: { target: EventTarget | null; preventDefault: () => void }) {
    if (workspaceBrowserOpen) {
      event.preventDefault();
      return;
    }
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: selectOpenCountRef.current > 0,
        msSinceSelectClose: Date.now() - selectClosedAtRef.current,
      })
    ) {
      event.preventDefault();
    }
  }
  // Pinned host/workspace. "" = none: a new task waits for a pick (it starts on
  // the first online host), and an older unpinned task keeps resolving its host
  // when it fires.
  const [hostId, setHostId] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  // When on, each fire runs in a FRESH managed sandbox (execution_target =
  // "managed_sandbox") instead of a connected host. Catalog-backed providers
  // offer repository selection instead of browsing a connected host.
  const [sandboxMode, setSandboxMode] = useState(false);
  const sandboxWorkspace = sandboxMode ? workspace : "";
  let savedWorkspace = editingTask?.workspace ?? "";
  if (
    editingTask?.executionTarget === "managed_sandbox" &&
    !isValidSandboxRepoUrl(splitSandboxWorkspace(savedWorkspace).url)
  ) {
    savedWorkspace = "";
  }
  const savedSandboxWorkspace =
    editingTask?.executionTarget === "managed_sandbox" ? savedWorkspace : "";
  // Project by name: null = unchanged; "" = no project.
  const [projectChoice, setProjectChoice] = useState<string | null>(null);
  // Id of the existing project picked, captured at pick time so a later rename
  // can't redirect the save; null for a newly typed name.
  const [pickedProjectId, setPickedProjectId] = useState<string | null>(null);
  const [sessionMode, setSessionMode] = useState<ScheduledTaskSessionMode>("new");
  const [startNewSession, setStartNewSession] = useState(false);
  const executionTarget: ScheduledTaskExecutionTarget = sandboxMode
    ? "managed_sandbox"
    : "connected_host";
  // Agent, host, execution-target and repository changes clear the binding.
  // Host directory edits stay locked: a symlink may resolve to the same directory.
  const reusesExistingSession =
    sessionMode === "continue" &&
    editingTask !== null &&
    editingTask.sessionId !== null &&
    !startNewSession &&
    !agentChanged &&
    executionTarget === editingTask.executionTarget &&
    (!sandboxMode || sandboxWorkspace === savedWorkspace) &&
    (hostId === "" || hostId === editingTask.hostId);
  // An active trigger template replaces the Prompt field; the saved prompt is
  // rendered from its fields. Create-only: an edit always shows the prompt.
  const [trigger, setTrigger] = useState<TriggerSource | null>(null);
  const [triggerFields, setTriggerFields] = useState<TriggerFields>(() =>
    emptyTriggerFields("jira"),
  );
  const [error, setError] = useState<string | null>(null);
  const [scheduleUnsupported, setScheduleUnsupported] = useState(false);
  const savedSchedule = useMemo(
    () => (editingTask ? parseRRuleToScheduleModel(editingTask.rrule) : null),
    [editingTask],
  );

  // Seed Name/Prompt on the closed→open transition ONLY. Keying off the
  // transition (not `open` being true) means we never clobber the user's edits
  // while the dialog stays open. Each fresh open is AUTHORITATIVE — the fields
  // are set to the initial values, or cleared to "" when none are supplied — so
  // a stale prefill can never leak into a subsequent manual open regardless of
  // how the prior instance was closed, and opening another template reseeds.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setAdvancedOpen(true);
      setTimezone(editingTask?.timezone ?? localTimezone());
      if (editingTask) {
        const parsedSchedule = savedSchedule;
        setName(editingTask.name);
        setPrompt(editingTask.prompt);
        setPickedAgentId(editingTask.agentId);
        // Prefill the model/effort/permission controls from the loaded task; null → "".
        setPickedModel(editingTask.modelOverride ?? "");
        setPickedEffort(editingTask.reasoningEffort ?? "");
        setPickedPermission(editingTask.permissionMode ?? "");
        setSchedule(parsedSchedule ?? DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(parsedSchedule === null);
        setHostId(editingTask.hostId ?? "");
        setWorkspace(savedWorkspace);
        setSandboxMode(editingTask.executionTarget === "managed_sandbox");
        setProjectChoice(null);
        setPickedProjectId(null);
        setSessionMode(editingTask.sessionMode);
        setStartNewSession(false);
        setTrigger(null);
      } else {
        setName(initialTrigger ? TRIGGER_TEMPLATES[initialTrigger].name : (initialName ?? ""));
        setPrompt(initialPrompt ?? "");
        setPickedAgentId(null);
        setPickedModel("");
        setPickedEffort("");
        setPickedPermission("");
        // The manager session must persist: it dedupes against its own workers.
        setSessionMode(initialTrigger ? "continue" : "new");
        setStartNewSession(false);
        setTrigger(initialTrigger);
        setTriggerFields(emptyTriggerFields(initialTrigger ?? "jira"));
        setSchedule(initialTrigger ? HOURLY_SCHEDULE_MODEL : DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(false);
        setHostId("");
        setWorkspace("");
        setSandboxMode(false);
        setProjectChoice(null);
        setPickedProjectId(null);
      }
      setError(null);
    }
    wasOpen.current = open;
  }, [
    open,
    initialName,
    initialPrompt,
    initialTrigger,
    editingTask,
    savedSchedule,
    savedWorkspace,
  ]);

  // A new task pins a host, starting on the first online one as New Chat does.
  const defaultHostId = hosts?.find((h) => h.status === "online")?.host_id;
  useEffect(() => {
    if (open && !isEdit && !sandboxMode && hostId === "" && defaultHostId !== undefined) {
      setHostId(defaultHostId);
    }
  }, [open, isEdit, sandboxMode, hostId, defaultHostId]);

  const savedProjectId = editingTask?.projectId ?? null;
  const savedProjectName =
    savedProjectId !== null ? projects.find((p) => p.id === savedProjectId)?.name : "";
  // A picked project is shown by its id, so a rename elsewhere shows the new name.
  const pickedProject =
    pickedProjectId !== null ? projects.find((p) => p.id === pickedProjectId) : undefined;
  const projectName = pickedProject?.name ?? projectChoice ?? savedProjectName ?? "";
  const projectIcon = (pickedProject ?? projects.find((p) => p.name === projectName))?.icon;
  // The saved project has no name while the list loads, if it fails to load,
  // or once the project is deleted. Only a loaded list proves it is deleted,
  // and a deleted one reads as "No project": runs already file nothing there.
  const savedProjectUnresolved = projectChoice === null && savedProjectName === undefined;
  const savedProjectDeleted = savedProjectUnresolved && projectsLoaded;
  const unresolvedProjectLabel = unresolvedProjectText(
    savedProjectUnresolved,
    savedProjectDeleted,
    projectsFailed,
  );

  const hostOptions = hosts ?? [];
  // The resolved Host for the pinned id, or undefined when none is pinned.
  const selectedHost = hostId === "" ? undefined : hostOptions.find((h) => h.host_id === hostId);
  const harnessSettings = useScheduledHarnessSettings(
    agents?.find((agent) => agent.id === effectiveAgentId),
    selectedHost,
    sandboxMode,
    pickedModel,
    open,
  );
  const supportsModel = harnessSettings.supportsModel;
  const supportsPermission = harnessSettings.permissions.length > 0;
  const effectivePermission =
    harnessSettings.harness !== "claude-native" && pickedPermission === "default"
      ? ""
      : pickedPermission;
  const originalTargetSelected =
    editingTask?.executionTarget === "managed_sandbox"
      ? sandboxMode
      : !sandboxMode && hostId === (editingTask?.hostId ?? "");
  const showAdvanced = supportsModel || supportsPermission;
  function handleTargetChange(value: string) {
    const nextSandbox = value === SANDBOX_HOST;
    const nextHostId = nextSandbox ? "" : value;
    if (nextSandbox === sandboxMode && nextHostId === hostId) return;
    const originalTarget =
      editingTask?.executionTarget === "managed_sandbox"
        ? nextSandbox
        : !nextSandbox && nextHostId === editingTask?.hostId;
    const restore = isEdit && effectiveAgentId === editingTask?.agentId && originalTarget;
    setPickedModel(restore ? (editingTask?.modelOverride ?? "") : "");
    setPickedEffort(restore ? (editingTask?.reasoningEffort ?? "") : "");
    setSandboxMode(nextSandbox);
    setHostId(nextHostId);
    // Host directories and sandbox repository URLs are different workspace inputs.
    if (nextSandbox !== sandboxMode) {
      const nextTarget = nextSandbox ? "managed_sandbox" : "connected_host";
      setWorkspace(editingTask?.executionTarget === nextTarget ? savedWorkspace : "");
    }
    setWorkspaceBrowserOpen(false);
  }
  function handleModelChange(model: string) {
    setPickedModel(model);
    if (
      pickedEffort &&
      !harnessSettings.effortsForModel(model).some((option) => option.value === pickedEffort)
    ) {
      setPickedEffort("");
    }
  }

  const workspaceHostOffline = !sandboxMode && selectedHost?.status === "offline";
  useEffect(() => {
    if (workspaceHostOffline) setWorkspaceBrowserOpen(false);
  }, [workspaceHostOffline]);
  // Host whose `configured_harnesses` drives the picker's "needs setup" badges.
  // Before a host is picked (or for an older unpinned task), fall back to the
  // first ONLINE host so unconfigured agents show "needs setup" right away.
  const badgeHost =
    selectedHost ?? hostOptions.find((h) => h.status === "online") ?? hostOptions[0];

  // A workspace is only valid with a host — mirror the server's pairing rule so
  // the user gets inline feedback instead of a 400. In sandbox mode there is no
  // host/workspace pairing at all, so the rule doesn't apply.
  const workspaceWithoutHost = !sandboxMode && workspace.trim() !== "" && hostId === "";
  // Metadata edits can preserve an expired schedule; rescheduling requires a future cutoff.
  const allowPastEnd =
    savedSchedule !== null &&
    timezone === editingTask?.timezone &&
    buildRRule(schedule) === buildRRule(savedSchedule);
  const requiresAnchoredScheduling = schedule.preset === "custom" || schedule.end?.type === "count";
  const unsupportedByScheduler = !anchoredScheduling && requiresAnchoredScheduling;
  const scheduleInvalid =
    scheduleUnsupported ||
    unsupportedByScheduler ||
    validateSchedule(schedule, { allowPastEnd }) !== null;
  const renderedTriggerPrompt =
    trigger !== null && triggerFieldsComplete(triggerFields)
      ? renderTriggerPrompt(trigger, triggerFields, {
          // Workers run where the manager runs: beside it on a connected host,
          // or each in its own sandbox.
          placement: sandboxMode ? "managed" : "same_host",
          sessionLink,
          model: supportsModel && pickedModel !== "" ? pickedModel : undefined,
          effort: supportsModel && pickedEffort !== "" ? pickedEffort : undefined,
        })
      : "";
  const effectivePrompt = trigger !== null ? renderedTriggerPrompt : prompt;
  // Set across the whole submit, which may first find or create a project.
  const [submitting, setSubmitting] = useState(false);
  const mutationPending = submitting || createMutation.isPending || updateMutation.isPending;
  const canSubmit =
    name.trim() !== "" &&
    effectivePrompt.trim() !== "" &&
    (isEdit || effectiveAgentId !== null) &&
    (isEdit || sandboxMode || hostId !== "") &&
    !workspaceWithoutHost &&
    !scheduleInvalid &&
    !harnessSettings.blockedReason &&
    !mutationPending;

  function resetForm() {
    setName("");
    setPrompt("");
    setPickedAgentId(null);
    setPickedModel("");
    setPickedEffort("");
    setPickedPermission("");
    setSchedule(DEFAULT_SCHEDULE_MODEL);
    setHostId("");
    setWorkspace("");
    setWorkspaceBrowserOpen(false);
    setSandboxMode(false);
    setProjectChoice(null);
    setPickedProjectId(null);
    setTrigger(null);
    setTriggerFields(emptyTriggerFields("jira"));
    setError(null);
    setScheduleUnsupported(false);
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetForm();
    onOpenChange(next);
  }

  async function handleSubmit() {
    if (harnessSettings.blockedReason) return;
    if (!editingTask && effectiveAgentId === null) return;
    setError(null);
    setSubmitting(true);
    try {
      // Send execution-target and sandbox repository fields only when changed.
      const executionTargetChanged = editingTask
        ? editingTask.executionTarget !== executionTarget
        : sandboxMode;
      const selectedWorkspace = workspace.trim();
      const workspaceChanged = editingTask && selectedWorkspace !== savedWorkspace;
      const canSelectSandboxWorkspace = sandboxMode && sandboxRepoCatalog.length > 0;
      const input = {
        name: name.trim(),
        prompt: effectivePrompt.trim(),
        rrule: buildRRule(schedule),
        timezone,
        ...(executionTargetChanged ? { executionTarget } : {}),
        ...(!sandboxMode && hostId !== "" ? { hostId } : {}),
        ...(canSelectSandboxWorkspace && (workspaceChanged || (!editingTask && selectedWorkspace))
          ? { workspace: selectedWorkspace || null }
          : {}),
        ...(!sandboxMode && hostId !== "" && selectedWorkspace !== ""
          ? { workspace: selectedWorkspace }
          : {}),
        ...((editingTask?.sessionMode ?? "new") !== sessionMode ? { sessionMode } : {}),
      };
      const projectInput = await projectChangeInput(
        // Saving over a deleted project clears it, matching the "No project" shown.
        projectChoice ?? (savedProjectDeleted ? "" : null),
        pickedProjectId,
        savedProjectId,
        resolveProjectId,
      );
      if (editingTask) {
        // Continued-session edits omit launch settings; new targets clear stale overrides.
        const overrides = reusesExistingSession
          ? {}
          : {
              ...(supportsModel || !originalTargetSelected
                ? {
                    modelOverride: pickedModel || null,
                    reasoningEffort: pickedEffort || null,
                  }
                : {}),
              ...(supportsPermission ? { permissionMode: effectivePermission || null } : {}),
            };
        await updateMutation.mutateAsync({
          id: editingTask.id,
          input: {
            ...input,
            ...overrides,
            ...projectInput,
            ...(sessionMode === "continue" && startNewSession && editingTask.sessionId !== null
              ? { sessionId: null }
              : {}),
            // Only on a real switch: sending the unchanged agent is a server-side
            // no-op, but omitting it keeps the PATCH honest about what changed.
            ...(agentChanged && effectiveAgentId !== null ? { agentId: effectiveAgentId } : {}),
          },
        });
      } else {
        if (effectiveAgentId === null) return;
        const templateId = initialTemplateId ?? initialTrigger;
        await createMutation.mutateAsync({
          ...(templateId ? { templateId } : {}),
          ...input,
          ...projectInput,
          agentId: effectiveAgentId,
          // Include an override only when the agent supports model/effort AND the
          // user picked a non-default value; an unselected control is omitted so
          // the create uses the agent's configured defaults.
          ...(supportsModel && pickedModel !== "" ? { modelOverride: pickedModel } : {}),
          ...(supportsModel && pickedEffort !== "" ? { reasoningEffort: pickedEffort } : {}),
          ...(supportsPermission && effectivePermission !== ""
            ? { permissionMode: effectivePermission }
            : {}),
        });
      }
      handleOpenChange(false);
    } catch (err) {
      setError(
        err instanceof ScheduledTaskApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : isEdit
              ? "Couldn't update the automation."
              : "Couldn't create the automation.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="flex max-h-[90vh] flex-col overflow-hidden p-0 sm:max-w-[560px]"
        data-testid="create-scheduled-task-dialog"
        // Keep a nested Select's dismiss (pick an option, OR click empty modal
        // body / trigger while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss` — it covers both the popper-target path and the
        // Select-open + focus-outside path, while leaving real backdrop clicks
        // and Escape to close as normal.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>{isEdit ? "Edit automation" : "New automation"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update this recurring agent session."
              : "Runs an agent session on a recurring schedule."}
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4"
          data-testid="scheduled-task-dialog-body"
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-name">Name</Label>
            <Input
              id="task-name"
              value={name}
              placeholder="daily-brief"
              data-testid="task-name-input"
              className="text-ui"
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          {sessionModesSupported && (
            <div className="flex flex-col gap-1.5" data-testid="task-session-mode-field">
              <Label htmlFor="task-session-mode">Session</Label>
              <Select
                value={sessionMode}
                componentId="tasks.scheduled.session_mode"
                disabled={trigger !== null}
                onValueChange={(v) => setSessionMode(v as ScheduledTaskSessionMode)}
                onOpenChange={handleSelectOpenChange}
              >
                <SelectTrigger
                  id="task-session-mode"
                  data-testid="task-session-mode-trigger"
                  className="w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  <SelectItem value="new">New session each run</SelectItem>
                  <SelectItem value="continue" data-testid="task-session-mode-continue">
                    Continue in the same session
                  </SelectItem>
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                {trigger !== null
                  ? "Trigger automations keep one session, which remembers its last check and the sessions it already started."
                  : sessionMode === "continue"
                    ? "The first run starts a session, and later runs send their prompt to it so the agent keeps its context, even while an earlier run is still working."
                    : "Each run starts a new session."}
              </p>
              {isEdit && sessionMode === "continue" && editingTask?.sessionId && (
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="task-start-new-session"
                    checked={startNewSession}
                    onCheckedChange={(checked) => setStartNewSession(checked === true)}
                    data-testid="task-start-new-session"
                    componentId="tasks.scheduled.start_new_session"
                  />
                  <label htmlFor="task-start-new-session" className="cursor-pointer text-sm">
                    Start a new session on the next run
                  </label>
                </div>
              )}
            </div>
          )}

          {trigger !== null ? (
            <TriggerTemplateFields
              source={trigger}
              fields={triggerFields}
              onChange={setTriggerFields}
              onSelectOpenChange={handleSelectOpenChange}
              renderedPrompt={renderedTriggerPrompt}
              onEditAsPrompt={() => {
                setPrompt(renderedTriggerPrompt);
                setTrigger(null);
              }}
            />
          ) : (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-prompt">Prompt</Label>
              <Textarea
                id="task-prompt"
                value={prompt}
                rows={3}
                placeholder="What should the agent do each run?"
                data-testid="task-prompt-input"
                componentId="tasks.scheduled.prompt"
                // No native resize grip — match the clean styling of the other fields.
                className="resize-none text-ui"
                onChange={(e) => setPrompt(e.target.value)}
              />
            </div>
          )}

          <ScheduleFields
            timezone={timezone}
            onTimezoneChange={setTimezone}
            model={schedule}
            anchoredScheduling={anchoredScheduling}
            recurrenceUnsupported={scheduleUnsupported}
            allowPastEnd={allowPastEnd}
            onChange={setSchedule}
            onRecurrenceSelect={() => setScheduleUnsupported(false)}
            onSelectOpenChange={handleSelectOpenChange}
          />
          {scheduleUnsupported && (
            <p className="text-sm text-destructive" role="alert">
              This schedule can&apos;t be edited in this form yet. Choose a frequency to replace it.
            </p>
          )}

          {unsupportedByScheduler && (
            <p className="text-sm text-destructive" role="alert">
              This server does not support custom schedules or occurrence limits. Choose a supported
              frequency and ending to replace this schedule.
            </p>
          )}

          <div className={projectsSupported ? "grid gap-3 sm:grid-cols-2 sm:gap-6" : "grid gap-3"}>
            {/* Pin a connected host, or provision a sandbox per run (kept for continuing sessions). */}
            <div className="flex min-w-0 flex-col gap-1.5" data-testid="task-host-field">
              <Label htmlFor="task-host">Host</Label>
              <Select
                value={sandboxMode ? SANDBOX_HOST : hostId}
                componentId="tasks.scheduled.host"
                onValueChange={handleTargetChange}
                onOpenChange={handleSelectOpenChange}
              >
                <SelectTrigger id="task-host" data-testid="task-host-trigger" className="w-full">
                  <SelectValue placeholder={isEdit ? UNPINNED_HOST_LABEL : "Pick a host"} />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {(managedSandboxesEnabled || sandboxMode) && (
                    <SelectItem value={SANDBOX_HOST} data-testid="task-host-sandbox-option">
                      {sandboxLabel}
                    </SelectItem>
                  )}
                  {hostOptions.map((host) => (
                    <SelectItem key={host.host_id} value={host.host_id}>
                      {host.name} {host.status === "offline" ? "(offline)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                {sandboxMode
                  ? sessionMode === "continue"
                    ? "Keeps one sandbox for every run, including a session started after a reset. Shutdown follows the server’s sandbox configuration."
                    : "Provisions a fresh sandbox for each run. Shutdown follows the server’s sandbox configuration."
                  : hostId === ""
                    ? isEdit
                      ? "Runs on the connected host you used most recently. Pick a host to pin it."
                      : "Pick the host this automation runs on."
                    : "Runs on this host. A run fails while the host is offline."}
              </p>
            </div>

            {projectsSupported && (
              <div className="flex min-w-0 flex-col gap-1.5" data-testid="task-project-field">
                <Label>Project (optional)</Label>
                <DropdownMenu onOpenChange={handleSelectOpenChange}>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="outline"
                      className="w-full justify-start gap-2"
                      data-testid="task-project-trigger"
                    >
                      {(projectName !== "" || unresolvedProjectLabel !== null) && (
                        <ProjectRowIcon icon={projectIcon} />
                      )}
                      <span className="flex-1 truncate text-left">
                        {unresolvedProjectLabel ?? (projectName || "No project")}
                      </span>
                      <ChevronDownIcon className="size-4 text-muted-foreground" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-56">
                    {/* The picker's "Remove from" row needs the name; this works without it. */}
                    {savedProjectUnresolved && !savedProjectDeleted && (
                      <DropdownMenuItem
                        className="px-2 py-1"
                        onSelect={() => {
                          setProjectChoice("");
                          setPickedProjectId(null);
                        }}
                      >
                        No project
                      </DropdownMenuItem>
                    )}
                    <ProjectPicker
                      currentProject={projectName || null}
                      onSelect={(next) => {
                        setProjectChoice(next);
                        setPickedProjectId(projects.find((p) => p.name === next)?.id ?? null);
                      }}
                    />
                  </DropdownMenuContent>
                </DropdownMenu>
                <p className="text-sm text-muted-foreground">
                  Each run&apos;s session is filed into this project. Type a new name to create one.
                </p>
              </div>
            )}
          </div>

          {sandboxMode && sandboxRepoCatalog.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-sandbox-repository">Repository</Label>
              <Select
                value={sandboxWorkspace || PROVIDER_DEFAULT}
                onValueChange={(value) => setWorkspace(value === PROVIDER_DEFAULT ? "" : value)}
                onOpenChange={handleSelectOpenChange}
                componentId="tasks.scheduled.sandbox_repository"
              >
                <SelectTrigger id="task-sandbox-repository" className="w-full">
                  <SelectValue placeholder="Provider default" />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  <SelectItem value={PROVIDER_DEFAULT}>Provider default</SelectItem>
                  {savedSandboxWorkspace &&
                    !sandboxRepoCatalog.some((repo) => repo.url === savedSandboxWorkspace) && (
                      <SelectItem value={savedSandboxWorkspace}>{savedSandboxWorkspace}</SelectItem>
                    )}
                  {sandboxRepoCatalog.map((repo) => (
                    <SelectItem key={repo.url} value={repo.url}>
                      {repo.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                Runs in this repository inside the sandbox.
              </p>
            </div>
          )}

          {!sandboxMode && hostId !== "" && (
            <div className="flex flex-col gap-1.5">
              <Label>Workspace (optional)</Label>
              <p id="task-workspace-help" className="text-sm text-muted-foreground">
                {workspaceHostOffline
                  ? "Connect your host to select a workspace."
                  : "Defaults to the host's home directory. Pick a directory to pin it."}
              </p>
              <Button
                type="button"
                variant="outline"
                className="w-full justify-start gap-2"
                disabled={workspaceHostOffline}
                aria-describedby="task-workspace-help"
                onClick={() => setWorkspaceBrowserOpen(true)}
                data-testid="task-workspace-browse"
              >
                <FolderOpenIcon className="size-4 text-muted-foreground" />
                <span className="truncate">{workspace || "Browse folders…"}</span>
              </Button>
              <WorkspacePickerDialog
                open={workspaceBrowserOpen && !workspaceHostOffline}
                onOpenChange={setWorkspaceBrowserOpen}
                hostId={hostId}
                initialPath={workspace}
                onConfirm={setWorkspace}
              />
              {workspace && (
                <p className="truncate font-mono text-sm text-muted-foreground">{workspace}</p>
              )}
            </div>
          )}

          {workspaceWithoutHost && (
            <p
              className="flex items-center gap-1.5 text-sm text-destructive"
              data-testid="workspace-without-host-error"
            >
              <TriangleAlertIcon className="size-3.5 shrink-0" />
              Pick a host before pinning a workspace.
            </p>
          )}

          <div className="flex flex-col gap-1.5">
            <Label>Harness</Label>
            <div data-testid="task-agent-picker">
              <AgentHarnessPicker
                agentEntries={agentEntries}
                harnessEntries={harnessEntries}
                effectiveAgentId={effectiveAgentId}
                agentLabel={agentLabel}
                hasAgents={agentList.length > 0}
                // Needs-setup badges use the pinned host, else the first online host.
                host={badgeHost}
                onSelectAgent={handleSelectAgent}
                pendingAgent={null}
                pendingAgentId="__unused_pending_agent__"
                onSelectPending={() => {}}
                // Custom-agent creation is inert until there is a way to
                // persist a new agent independently of creating a session.
                onCreateCustomAgent={() => {}}
                sandboxSelected={false}
                // Forward the dropdown open/close into the dialog's outside-click
                // dismiss guard so opening the picker doesn't close the modal.
                onOpenChange={handleSelectOpenChange}
                // This picker is nested inside a Dialog. Radix DropdownMenu's
                // default modal mode can turn an inside-dialog click into a
                // parent Dialog outside interaction while the menu dismisses.
                dropdownModal={false}
                // The shared menu caps against available viewport height, so
                // constrained screens scroll without clipping taller screens.
                contentClassName="w-80"
                // Full-width trigger → left-align the menu's edge to it.
                contentAlign="start"
                // Match the Host select's full-width, bordered h-8 styling.
                triggerClassName="h-8 w-full justify-between rounded-lg border border-input bg-transparent px-2.5 text-foreground hover:bg-transparent hover:text-foreground dark:bg-input"
                triggerLabelClassName="max-w-none text-ui"
              />
            </div>
            {agentChanged && (
              <p className="text-sm text-muted-foreground">
                Future runs use {agentLabel}; past runs keep the agent they ran with
              </p>
            )}
            {!showAdvanced && (
              <p className="text-sm text-muted-foreground">
                Uses this agent&apos;s default model, effort, and permission settings
              </p>
            )}
          </div>

          {harnessSettings.blockedReason && (
            <p
              role="alert"
              data-testid="task-harness-unsupported"
              className="text-sm text-destructive"
            >
              {harnessSettings.blockedReason}
            </p>
          )}
          {/* Each harness exposes only the launch settings it supports. */}
          {showAdvanced && (
            <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
              <CollapsibleTrigger className="group flex cursor-pointer items-center gap-2 rounded-sm py-1 text-muted-foreground text-ui outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                Advanced
                <ChevronDownIcon className="size-4 transition-transform group-data-[state=open]:rotate-180" />
              </CollapsibleTrigger>
              <CollapsibleContent className="pt-3" data-testid="task-model-effort-field">
                <ModelEffortFields
                  model={reusesExistingSession ? (editingTask?.modelOverride ?? "") : pickedModel}
                  effort={
                    reusesExistingSession ? (editingTask?.reasoningEffort ?? "") : pickedEffort
                  }
                  permissionMode={
                    reusesExistingSession
                      ? (editingTask?.permissionMode ?? "")
                      : effectivePermission
                  }
                  settings={harnessSettings}
                  disabled={reusesExistingSession}
                  onModelChange={handleModelChange}
                  onEffortChange={setPickedEffort}
                  onPermissionModeChange={setPickedPermission}
                  onSelectOpenChange={handleSelectOpenChange}
                />
                {reusesExistingSession ? (
                  <div
                    className="mt-3 flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-foreground"
                    data-testid="task-session-settings-note"
                    role="status"
                  >
                    <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                    <p>
                      To change model, effort, or permission mode, edit the existing session
                      directly.
                    </p>
                  </div>
                ) : (
                  <p className="mt-1.5 text-sm text-muted-foreground">
                    Leave on Default to use the agent&apos;s configured model, effort, and
                    permission mode. Automations run unattended; modes that require approval may
                    pause until you respond.
                  </p>
                )}
              </CollapsibleContent>
            </Collapsible>
          )}

          {error && (
            <div
              role="alert"
              data-testid="create-error"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t-0 bg-transparent px-6 py-4 sm:justify-end">
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            componentId="tasks.scheduled.cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            loading={mutationPending}
            disabled={!canSubmit}
            data-testid="create-scheduled-task-submit"
            componentId="tasks.scheduled.save"
          >
            {isEdit ? "Save changes" : "Create automation"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Label for a saved project the list can't name yet; `null` once named or deleted. */
function unresolvedProjectText(
  unresolved: boolean,
  deleted: boolean,
  failed: boolean,
): string | null {
  if (!unresolved || deleted) return null;
  if (failed) return "Project unavailable";
  return "Loading…";
}

/**
 * The project field to send for a pick, and nothing when it is unchanged. An
 * existing project is sent by the id captured when it was picked; a typed name
 * is created (or found) first, as moving a session does.
 */
async function projectChangeInput(
  choice: string | null,
  pickedId: string | null,
  savedProjectId: string | null,
  resolveProjectId: (name: string) => Promise<string>,
): Promise<{ projectId?: string | null }> {
  if (choice === null) return {};
  if (choice === "") return savedProjectId !== null ? { projectId: null } : {};
  if (pickedId !== null) return pickedId === savedProjectId ? {} : { projectId: pickedId };
  return { projectId: await resolveProjectId(choice) };
}

const HOURLY_SCHEDULE_MODEL: ScheduleModel = { ...DEFAULT_SCHEDULE_MODEL, preset: "hourly" };

/** How an older task with no pinned host reads; the task detail page uses the same words. */
const UNPINNED_HOST_LABEL = "Most recently active host";

/** Sentinel Select value for the "New Sandbox" option (execution_target=managed_sandbox). */
const SANDBOX_HOST = "__sandbox__";

// The nested-dropdown dismiss guard now lives in a dependency-free module so
// other dialogs (project settings) can reuse it without pulling this file's
// module graph. Re-exported here to keep existing imports and tests stable.
export { isBackdropOverlay, isInsidePopper, shouldGuardDialogDismiss };
