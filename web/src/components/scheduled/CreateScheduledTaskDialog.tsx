import { ScheduleSelect as Select } from "./ScheduleSelect";
import { useScheduleEscape } from "./useScheduleEscape";
// Dialog for creating or editing a scheduled task. Reuses the existing agent,
// host, and workspace pickers where the backend can persist those fields.

import { cn } from "@/lib/utils";
import { pickerTriggerClass } from "./pickerStyles";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDownIcon, ClockIcon, FolderOpenIcon, TriangleAlertIcon } from "lucide-react";
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
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Label } from "@/components/scheduled/Label";
import { ScheduleFields } from "@/components/scheduled/ScheduleFields";
import {
  acceptsApprovalSkippingModes,
  useScheduledHarnessSettings,
} from "./useScheduledHarnessSettings";
import { ModelEffortFields } from "@/components/scheduled/ModelEffortFields";
import { TriggerTemplateFields } from "@/components/scheduled/TriggerTemplateFields";
import {
  TriggerWorkerHarnessField,
  TriggerWorkerSettingsFields,
} from "./TriggerWorkerSettingsFields";
import {
  combineTriggerPrompt,
  emptyTriggerFields,
  isTaskSessionTriggerPrompt,
  isTriggerPrompt,
  readTriggerWorkerPlacement,
  readTriggerWorkerSettings,
  renderTriggerPrompt,
  splitTriggerPrompt,
  TRIGGER_SOURCES,
  TRIGGER_TEMPLATES,
  triggerFieldsComplete,
  triggerPromptSource,
  triggerServiceName,
  updateTriggerWorkerSettings,
  type TriggerFields,
  type TriggerTaskSessionMode,
  type TriggerWorkerSettings,
  type TriggerSource,
  type WorkerPlacement,
} from "@/components/scheduled/triggerTemplates";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { ProjectPicker, ProjectRowIcon } from "@/shell/ProjectPicker";
import { useProjects, useResolveOrCreateProjectId } from "@/hooks/useConversations";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isBootFallbackInfo } from "@/lib/bootCapabilities";
import { isFeatureEnabled, pickerCuration, sandboxOptionLabel } from "@/lib/capabilities";
import { useCreateScheduledTask, useUpdateScheduledTask } from "@/hooks/useScheduledTasks";
import { partitionPickerEntries, selectableSessionAgents } from "@/lib/agentGrouping";
import {
  APPROVAL_SKIPPING_PERMISSION_MODES,
  NEW_AUTOMATION_PERMISSION_MODES,
} from "@/lib/nativeHarnessModes";
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
  RANDOM_WORKTREE_BASE_REF,
  type ScheduledTask,
  type AutomationTemplateId,
  type ScheduledTaskExecutionTarget,
  type ScheduledTaskSessionMode,
} from "@/lib/scheduledTasksApi";
import {
  isValidSandboxRepoUrl,
  SANDBOX_REPO_LABEL_KEY,
  splitSandboxWorkspace,
} from "@/lib/sandboxRepositories";
import { localTimezone } from "@/lib/timezones";
import { describeSchedule } from "@/lib/scheduleText";
import { Link } from "@/lib/routing";
import { useSession } from "@/hooks/useSession";
import { claudePermissionModeFromSession } from "@/lib/claudePermissionMode";
import { codexApprovalModeFromSession } from "@/lib/codexApprovalMode";
import type { AutomationSourceSession } from "@/lib/automationFromSession";
import type { Session } from "@/lib/types";

const PROVIDER_DEFAULT = "provider-default";
const DEFAULT_AUTOMATION_PROJECT = "Omnigent Automation";

type AutomationTriggerType = "schedule" | TriggerSource;
const AUTOMATION_TRIGGER_TYPES: readonly AutomationTriggerType[] = ["schedule", ...TRIGGER_SOURCES];

/** A Trigger type option: the integration's brand mark, or a clock for a schedule. */
function TriggerTypeOption({ type }: { type: AutomationTriggerType }) {
  if (type === "schedule") {
    return (
      <>
        <ClockIcon className="text-muted-foreground" />
        Schedule
      </>
    );
  }
  const { icon: Icon, iconClassName } = TRIGGER_TEMPLATES[type];
  return (
    <>
      <Icon className={iconClassName} />
      {triggerServiceName(type)}
    </>
  );
}

/** The Trigger type field; create mode offers a schedule, a trigger edit only integrations. */
function TriggerTypeSelect<Type extends AutomationTriggerType>({
  value,
  types,
  isTypeDisabled,
  onChange,
  onOpenChange,
  disabled,
  children,
}: {
  value: Type;
  types: readonly Type[];
  isTypeDisabled?: (type: Type) => boolean;
  onChange: (type: Type) => void;
  onOpenChange: (isOpen: boolean) => void;
  disabled: boolean;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="task-trigger-type">Trigger type</Label>
      <Select
        value={value}
        onValueChange={(next) => onChange(next as Type)}
        onOpenChange={onOpenChange}
        disabled={disabled}
        componentId="tasks.scheduled.trigger_type"
      >
        <SelectTrigger id="task-trigger-type" data-testid="task-trigger-type" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="start">
          {types.map((type) => (
            <SelectItem
              key={type}
              value={type}
              disabled={isTypeDisabled?.(type) ?? false}
              data-testid={`task-trigger-type-${type}`}
            >
              <TriggerTypeOption type={type} />
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {children}
    </div>
  );
}

/** A source's draft while editing a trigger: its fields, and its prompt editor once edited as a prompt. */
interface EditSourceDraft {
  fields: TriggerFields;
  promptEditor: { prompt: string; systemPrompt: string | null } | null;
}

/** Restore a saved trigger prompt, applying worker settings changed while switched away. */
function restoreSourcePrompt(
  userPrompt: string,
  systemPrompt: string | null,
  workerSettings: TriggerWorkerSettings | null,
): { userPrompt: string; systemPrompt: string | null } {
  const restored = { userPrompt, systemPrompt };
  if (workerSettings === null || systemPrompt === null) return restored;
  const combined = combineTriggerPrompt(userPrompt, systemPrompt);
  const saved = readTriggerWorkerSettings(combined);
  if (
    saved === null ||
    (saved.agentId === workerSettings.agentId &&
      saved.model === workerSettings.model &&
      saved.effort === workerSettings.effort &&
      saved.permissionMode === workerSettings.permissionMode)
  ) {
    return restored;
  }
  const updated = updateTriggerWorkerSettings(combined, workerSettings);
  return updated === null ? restored : splitTriggerPrompt(updated);
}

interface AutomationTypeDraft {
  name: string;
  prompt: string;
  systemPrompt: string | null;
  trigger: TriggerSource | null;
  triggerFields: TriggerFields;
  schedule: ScheduleModel;
  timezone: string;
  sessionMode: ScheduledTaskSessionMode;
}

export function CreateScheduledTaskDialog({
  open,
  onOpenChange,
  initialName,
  initialPrompt,
  initialTrigger = null,
  initialTemplateId,
  sessionLink = "",
  editingTask = null,
  sourceSession = null,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Seed values applied when opening from a template card. */
  initialName?: string;
  initialPrompt?: string;
  /** Open on a Jira/Slack/GitHub/Linear trigger template instead of a plain prompt. */
  initialTrigger?: TriggerSource | null;
  initialTemplateId?: AutomationTemplateId;
  /** Shareable session URL the trigger prompt links back with; see `SESSION_LINK_ID_PLACEHOLDER`. */
  sessionLink?: string;
  editingTask?: ScheduledTask | null;
  /** Create the automation from this session: every run starts from a copy of it. */
  sourceSession?: AutomationSourceSession | null;
  /** Called once with the new automation after it is created (not after an edit). */
  onCreated?: (task: ScheduledTask) => void;
}) {
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts, isPending: hostsPending } = useHosts({ enabled: open });
  const info = useServerInfo();
  const anchoredScheduling = isFeatureEnabled(info, "automations_v2");
  const subhourSupported = info !== "loading" && info.scheduled_task_subhour === true;
  const triggerPollMinutes = info !== "loading" ? (info.automation_trigger_poll_minutes ?? 10) : 10;
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
  const sandboxAutomationNotice =
    info !== "loading" && info.sandbox_provider
      ? (info.sandbox_provider_automation_notices?.[info.sandbox_provider] ?? null)
      : null;

  const {
    data: projects = [],
    isSuccess: projectsLoaded,
    isError: projectsFailed,
  } = useProjects({ enabled: open && projectsSupported });
  const resolveProjectId = useResolveOrCreateProjectId();
  const worktreesSupported = info !== "loading" && info.scheduled_task_worktrees === true;
  const sessionModesSupported = info !== "loading" && info.scheduled_task_session_modes === true;
  const triggersSupported =
    sessionModesSupported && isFeatureEnabled(info, "session_orchestration");
  // Provider-named label for the sandbox host option (e.g. "Modal Sandbox"),
  // falling back to the generic "New Sandbox" — same helper the New Chat picker uses.
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  const createMutation = useCreateScheduledTask();
  const updateMutation = useUpdateScheduledTask();
  const isEdit = editingTask !== null;
  // Runs start from a copy of a session: the one being copied on create, or the
  // saved session copy on edit. They use a schedule and start each run in a new
  // session.
  const fromSession = isEdit ? null : sourceSession;
  const startsFromSessionId = fromSession?.id ?? editingTask?.sourceSessionId ?? null;
  const startsFromSession = startsFromSessionId !== null;
  const [sourceSnapshot, setSourceSnapshot] = useState<Session | null>(null);
  // A task saved from a trigger template keeps its one session on edit too; only
  // the reset below stays, for a session that's broken or stale.
  const editingTemplateTrigger =
    editingTask !== null &&
    editingTask.sessionMode === "continue" &&
    isTriggerPrompt(editingTask.prompt);
  // Temporary prompt edits must not expose controls that can rebind the poller.
  const savedWorkerSettings = editingTemplateTrigger
    ? readTriggerWorkerSettings(editingTask.prompt)
    : null;
  const editingWorkerSettings = savedWorkerSettings !== null;
  // A switch re-renders the workers, so it needs the saved worker settings to carry over.
  const editingTriggerSource =
    editingTask !== null && editingWorkerSettings ? triggerPromptSource(editingTask.prompt) : null;
  const savedWorkerPlacement =
    editingTask !== null && editingWorkerSettings
      ? readTriggerWorkerPlacement(editingTask.prompt)
      : null;

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState<string | null>(null);
  const [systemPromptOpen, setSystemPromptOpen] = useState(false);
  const [editingSystemPrompt, setEditingSystemPrompt] = useState(false);
  const promptFieldsRef = useRef<HTMLDivElement>(null);
  const [schedule, setSchedule] = useState<ScheduleModel>(DEFAULT_SCHEDULE_MODEL);
  const [timezone, setTimezone] = useState(localTimezone);

  // Empty overrides use the agent's configured defaults when a run starts.
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [pickedModel, setPickedModel] = useState<string>("");
  const [pickedEffort, setPickedEffort] = useState<string>("");
  const [pickedPermission, setPickedPermission] = useState<string>("");
  // "Default" and untouched are both "", so track the user's choice separately.
  const [permissionPicked, setPermissionPicked] = useState(false);

  const curation = pickerCuration(info);
  const curationPending = info === "loading" || isBootFallbackInfo(info);
  const agentList = useMemo(
    () => selectableSessionAgents(agents ?? [], curation.restrictToAgentNames),
    [agents, curation.restrictToAgentNames],
  );
  const { harnesses: harnessEntries, agents: agentEntries } = useMemo(
    () => partitionPickerEntries(agentList),
    [agentList],
  );
  // Resolve the effective selection: the explicit pick if it's still in the
  // list, else the edited task's own agent (which may be hidden from the picker
  // — never silently retarget it), else the deployment default or first agent.
  const defaultAgentId =
    agentList.find((agent) => agent.name === curation.defaultAgentName)?.id ??
    agentList[0]?.id ??
    null;
  let effectiveAgentId: string | null = defaultAgentId;
  if (agentList.some((agent) => agent.id === pickedAgentId)) effectiveAgentId = pickedAgentId;
  else if (editingTask) effectiveAgentId = editingTask.agentId;
  // The session's own agent, prefilled as the pick, even when the picker hides it.
  else if (fromSession?.agentId && pickedAgentId === fromSession.agentId)
    effectiveAgentId = fromSession.agentId;
  const selectedAgent = agents?.find((agent) => agent.id === effectiveAgentId);
  let agentLabel = "Select agent";
  if (selectedAgent) agentLabel = selectedAgent.display_name;
  else if (fromSession) agentLabel = fromSession.agentName ?? fromSession.agentId ?? agentLabel;
  else if (isEdit && editingTask) agentLabel = editingTask.agentId;
  // Editing a task only rebinds the agent when the user actually picks a
  // different one — the prefill starts equal to the task's own agent.
  const agentChanged =
    isEdit && effectiveAgentId !== null && effectiveAgentId !== editingTask?.agentId;

  // Set once the session's settings are applied, or once the user picks their own.
  const sourcePrefilledRef = useRef(false);

  function handleSelectAgent(agent: AvailableAgent) {
    sourcePrefilledRef.current = true;
    setPickedAgentId(agent.id);
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
    setPermissionPicked(false);
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
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  function changeAgentMenuOpen(next: boolean) {
    setAgentMenuOpen(next);
    handleSelectOpenChange(next);
  }
  useScheduleEscape(open && agentMenuOpen, () => {
    changeAgentMenuOpen(false);
    return true;
  });
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  function changeProjectMenuOpen(next: boolean) {
    setProjectMenuOpen(next);
    handleSelectOpenChange(next);
  }
  useScheduleEscape(open && projectMenuOpen, () => {
    changeProjectMenuOpen(false);
    return true;
  });
  function resetMenus() {
    setWorkspaceBrowserOpen(false);
    setProjectMenuOpen(false);
    setAgentMenuOpen(false);
    selectOpenCountRef.current = 0;
    selectClosedAtRef.current = 0;
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
  const [baseBranch, setBaseBranch] = useState<string | null>(null);
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
    baseBranch === (editingTask.baseBranch ?? null) &&
    (hostId === "" || hostId === editingTask.hostId);
  // An active trigger template replaces the Prompt field; the saved prompt is
  // rendered from its fields. An edit shows the saved prompt unless the user
  // switches the trigger to another integration.
  const [trigger, setTrigger] = useState<TriggerSource | null>(null);
  const [triggerType, setTriggerType] = useState<AutomationTriggerType>("schedule");
  const typeDrafts = useRef<Partial<Record<AutomationTriggerType, AutomationTypeDraft>>>({});
  const [triggerFields, setTriggerFields] = useState<TriggerFields>(() =>
    emptyTriggerFields("jira"),
  );
  // Like the model and effort overrides, this Advanced choice outlives switching trigger types.
  const [triggerTaskSessionMode, setTriggerTaskSessionMode] =
    useState<TriggerTaskSessionMode>("new");
  // Kept while a trigger edit watches another source, so switching back restores the saved prompt.
  const [sourceSwitch, setSourceSwitch] = useState<{
    source: TriggerSource;
    savedPrompt: string;
    savedSystemPrompt: string | null;
    savedStartNewSession: boolean;
  } | null>(null);
  const editSourceDrafts = useRef<Partial<Record<TriggerSource, EditSourceDraft>>>({});
  const [switchedWorkerSettings, setSwitchedWorkerSettings] =
    useState<TriggerWorkerSettings | null>(null);
  const [switchedWorkerPlacement, setSwitchedWorkerPlacement] = useState<WorkerPlacement | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [scheduleUnsupported, setScheduleUnsupported] = useState(false);
  const defaultTriggerSchedule = useMemo(
    () =>
      anchoredScheduling && (subhourSupported || triggerPollMinutes >= 60)
        ? { ...TRIGGER_SCHEDULE_MODEL, interval: triggerPollMinutes }
        : HOURLY_SCHEDULE_MODEL,
    [anchoredScheduling, subhourSupported, triggerPollMinutes],
  );
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
      typeDrafts.current = {};
      setSourceSwitch(null);
      editSourceDrafts.current = {};
      setSwitchedWorkerSettings(null);
      setSwitchedWorkerPlacement(null);
      setTriggerType(sourceSession ? "schedule" : (initialTrigger ?? "schedule"));
      resetMenus();
      setAdvancedOpen(false);
      // A source switch keeps the saved trigger's task-session choice.
      setTriggerTaskSessionMode(
        editingTask && isTaskSessionTriggerPrompt(editingTask.prompt) ? "continue" : "new",
      );
      setSystemPromptOpen(false);
      setEditingSystemPrompt(false);
      const parts = splitTriggerPrompt(editingTask?.prompt ?? initialPrompt ?? "");
      setPrompt(parts.userPrompt);
      setSystemPrompt(parts.systemPrompt);
      setTimezone(editingTask?.timezone ?? localTimezone());
      if (editingTask) {
        const parsedSchedule = savedSchedule;
        setName(editingTask.name);
        setPickedAgentId(editingTask.agentId);
        // Prefill the model/effort/permission controls from the loaded task; null → "".
        setPickedModel(editingTask.modelOverride ?? "");
        setPickedEffort(editingTask.reasoningEffort ?? "");
        setPickedPermission(editingTask.permissionMode ?? "");
        setSchedule(parsedSchedule ?? DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(parsedSchedule === null);
        setHostId(editingTask.hostId ?? "");
        setWorkspace(savedWorkspace);
        setBaseBranch(editingTask.baseBranch ?? null);
        setSandboxMode(editingTask.executionTarget === "managed_sandbox");
        setProjectChoice(null);
        setPickedProjectId(null);
        setSessionMode(editingTask.sessionMode);
        setStartNewSession(false);
        setTrigger(null);
      } else {
        setName(
          initialTrigger
            ? TRIGGER_TEMPLATES[initialTrigger].name
            : (sourceSession?.title ?? initialName ?? ""),
        );
        setPickedAgentId(sourceSession?.agentId ?? null);
        setPickedModel("");
        setPickedEffort("");
        setPickedPermission("");
        setPermissionPicked(false);
        // The manager session must persist: it dedupes against its own workers.
        setSessionMode(initialTrigger && !sourceSession ? "continue" : "new");
        setStartNewSession(false);
        setTrigger(sourceSession ? null : initialTrigger);
        setTriggerFields(emptyTriggerFields(initialTrigger ?? "jira"));
        setSchedule(
          initialTrigger && !sourceSession ? defaultTriggerSchedule : DEFAULT_SCHEDULE_MODEL,
        );
        setScheduleUnsupported(false);
        setHostId("");
        setWorkspace("");
        setBaseBranch(null);
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
    defaultTriggerSchedule,
    editingTask,
    savedSchedule,
    savedWorkspace,
    sourceSession,
  ]);

  // A new task starts on a server sandbox when one is offered, so it doesn't
  // depend on the creator's machine being online at fire time; otherwise it pins
  // the first online host as New Chat does. The target is chosen once, after
  // both /v1/info and the host list settle, so it never flips between the two.
  // With no sandbox and no online host it stays open until a host comes online.
  const targetChosenRef = useRef(false);
  useEffect(() => {
    if (open) targetChosenRef.current = false;
  }, [open]);
  const defaultHostId = hosts?.find((h) => h.status === "online")?.host_id;
  const targetInputsLoaded = !curationPending && !hostsPending;
  // From a session: a sandbox when the offered provider lists repositories (with the
  // session's repository), else the session's own connected host and directory, else
  // the first online host. Providers without a repository list are never the default.
  const catalogSandboxOffered =
    managedSandboxesEnabled &&
    !!info.sandbox_provider &&
    (info.sandbox_provider_capabilities?.[info.sandbox_provider]?.repo_catalog ?? []).length > 0;
  const sourceHostId = hosts?.some((h) => h.host_id === fromSession?.hostId)
    ? (fromSession?.hostId ?? undefined)
    : undefined;
  // The first repository's indexed label, or the joined label, which a long list omits.
  const sourceSandboxRepo = splitSandboxWorkspace(
    fromSession?.labels[`${SANDBOX_REPO_LABEL_KEY}.0`] ??
      (fromSession?.labels[SANDBOX_REPO_LABEL_KEY] ?? "").split(/\s+/)[0] ??
      "",
  ).url;
  const sourceRepoInCatalog = sandboxRepoCatalog.some((repo) => repo.url === sourceSandboxRepo);
  const sourceWorkspace = fromSession?.workspace ?? "";
  useEffect(() => {
    if (!open || isEdit || !targetInputsLoaded || targetChosenRef.current) return;
    if (fromSession) {
      if (catalogSandboxOffered) {
        targetChosenRef.current = true;
        setSandboxMode(true);
        setHostId("");
        setWorkspace(sourceRepoInCatalog ? sourceSandboxRepo : "");
      } else if (sourceHostId !== undefined) {
        targetChosenRef.current = true;
        setHostId(sourceHostId);
        setWorkspace(sourceWorkspace);
      } else if (defaultHostId !== undefined) {
        targetChosenRef.current = true;
        setHostId(defaultHostId);
      }
      return;
    }
    if (managedSandboxesEnabled) {
      targetChosenRef.current = true;
      setSandboxMode(true);
      setHostId("");
      setWorkspace("");
    } else if (defaultHostId !== undefined) {
      targetChosenRef.current = true;
      setHostId(defaultHostId);
    }
  }, [
    open,
    isEdit,
    targetInputsLoaded,
    managedSandboxesEnabled,
    defaultHostId,
    fromSession,
    catalogSandboxOffered,
    sourceHostId,
    sourceWorkspace,
    sourceRepoInCatalog,
    sourceSandboxRepo,
  ]);

  const savedProjectId = editingTask?.projectId ?? null;
  const savedProjectName =
    savedProjectId !== null ? projects.find((p) => p.id === savedProjectId)?.name : "";
  // A picked project is shown by its id, so a rename elsewhere shows the new name.
  const pickedProject =
    pickedProjectId !== null ? projects.find((p) => p.id === pickedProjectId) : undefined;
  const effectiveProjectChoice =
    projectChoice ?? (!isEdit && projectsSupported ? DEFAULT_AUTOMATION_PROJECT : null);
  const projectName = pickedProject?.name ?? effectiveProjectChoice ?? savedProjectName ?? "";
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
  function handleWorkerSettingsChange(settings: TriggerWorkerSettings) {
    if (trigger !== null) {
      setSwitchedWorkerSettings(settings);
      return;
    }
    const updated = updateTriggerWorkerSettings(effectivePrompt, settings);
    if (updated === null) return;
    const parts = splitTriggerPrompt(updated);
    setPrompt(parts.userPrompt);
    setSystemPrompt(parts.systemPrompt);
  }

  let workerSettings: TriggerWorkerSettings | null = null;
  if (editingWorkerSettings) {
    workerSettings =
      trigger !== null
        ? switchedWorkerSettings
        : readTriggerWorkerSettings(combineTriggerPrompt(prompt, systemPrompt));
  }
  let workerPlacement: WorkerPlacement | null = null;
  if (editingWorkerSettings) {
    workerPlacement =
      trigger !== null
        ? switchedWorkerPlacement
        : readTriggerWorkerPlacement(combineTriggerPrompt(prompt, systemPrompt));
  }
  const workerSandboxMode = workerPlacement === "managed" || sandboxMode;
  const workerAgent = agents?.find(
    (agent) => agent.id === (workerSettings?.agentId ?? editingTask?.agentId),
  );
  const workerHarnessSettings = useScheduledHarnessSettings(
    workerAgent,
    workerSandboxMode ? undefined : selectedHost,
    workerSandboxMode,
    workerSettings?.model ?? "",
    open && workerSettings !== null,
  );
  const harnessSettings = useScheduledHarnessSettings(
    agents?.find((agent) => agent.id === effectiveAgentId),
    selectedHost,
    sandboxMode,
    pickedModel,
    open,
  );
  const supportsModel = harnessSettings.supportsModel;
  const supportsPermission = harnessSettings.permissions.length > 0;
  const approvalSkippingAccepted = acceptsApprovalSkippingModes(info);
  // A saved mode the server would refuse edits as the harness default, so saving sends null.
  const editablePermissionMode = (mode: string | null | undefined): string =>
    mode && (approvalSkippingAccepted || !APPROVAL_SKIPPING_PERMISSION_MODES.has(mode)) ? mode : "";
  const newAutomationPermission =
    !isEdit && !permissionPicked && harnessSettings.harness
      ? NEW_AUTOMATION_PERMISSION_MODES[harnessSettings.harness]
      : undefined;
  const permissionChoice =
    newAutomationPermission &&
    harnessSettings.permissions.some((option) => option.value === newAutomationPermission)
      ? newAutomationPermission
      : editablePermissionMode(pickedPermission);
  const effectivePermission =
    harnessSettings.harness !== "claude-native" && permissionChoice === "default"
      ? ""
      : permissionChoice;
  const sessionPermissionModesSupported =
    info !== "loading" && info.session_create_permission_modes === true;
  // A new template's workers run the polling session's agent, so its mode applies to them,
  // except one that skips approval, which the server refuses for a session an agent starts.
  const workerPermissionMode =
    sessionPermissionModesSupported &&
    effectivePermission !== "" &&
    !APPROVAL_SKIPPING_PERMISSION_MODES.has(effectivePermission)
      ? effectivePermission
      : undefined;
  const originalTargetSelected =
    editingTask?.executionTarget === "managed_sandbox"
      ? sandboxMode
      : !sandboxMode && hostId === (editingTask?.hostId ?? "");
  const showAdvanced = supportsModel || supportsPermission;
  // From a session: start from its model, effort and permission mode once its
  // snapshot and the harness options are known; unattended rules still apply.
  useEffect(() => {
    if (open) sourcePrefilledRef.current = false;
  }, [open]);
  const sourceSettings = sourceSnapshot?.id === fromSession?.id ? sourceSnapshot : null;
  const harnessName = harnessSettings.harness;
  const harnessPermissions = harnessSettings.permissions;
  useEffect(() => {
    if (!open || sourceSettings === null || !harnessName || sourcePrefilledRef.current) return;
    // The session's settings belong to its own agent's harness.
    if (effectiveAgentId !== fromSession?.agentId) return;
    sourcePrefilledRef.current = true;
    if (supportsModel) {
      setPickedModel(sourceSettings.modelOverride ?? "");
      setPickedEffort(sourceSettings.reasoningEffort ?? "");
    }
    let mode: string | null = null;
    if (harnessName === "claude-native") mode = claudePermissionModeFromSession(sourceSettings);
    else if (harnessName === "codex-native") mode = codexApprovalModeFromSession(sourceSettings);
    if (
      mode &&
      (approvalSkippingAccepted || !APPROVAL_SKIPPING_PERMISSION_MODES.has(mode)) &&
      harnessPermissions.some((option) => option.value === mode)
    ) {
      setPickedPermission(mode);
      setPermissionPicked(true);
    }
  }, [
    open,
    sourceSettings,
    harnessName,
    harnessPermissions,
    supportsModel,
    effectiveAgentId,
    fromSession?.agentId,
    approvalSkippingAccepted,
  ]);
  function handleTargetChange(value: string) {
    const nextSandbox = value === SANDBOX_HOST;
    const nextHostId = nextSandbox ? "" : value;
    targetChosenRef.current = true;
    if (nextSandbox === sandboxMode && nextHostId === hostId) return;
    const originalTarget =
      editingTask?.executionTarget === "managed_sandbox"
        ? nextSandbox
        : !nextSandbox && nextHostId === editingTask?.hostId;
    const restore = isEdit && effectiveAgentId === editingTask?.agentId && originalTarget;
    const nextTarget = nextSandbox ? "managed_sandbox" : "connected_host";
    let nextWorkspace = workspace;
    if (nextSandbox !== sandboxMode) {
      nextWorkspace = editingTask?.executionTarget === nextTarget ? savedWorkspace : "";
    }
    setPickedModel(restore ? (editingTask?.modelOverride ?? "") : "");
    setPickedEffort(restore ? (editingTask?.reasoningEffort ?? "") : "");
    setBaseBranch(
      restore && !nextSandbox && nextWorkspace === savedWorkspace
        ? (editingTask?.baseBranch ?? null)
        : null,
    );
    setSandboxMode(nextSandbox);
    setHostId(nextHostId);
    // Host directories and sandbox repository URLs are different workspace inputs.
    setWorkspace(nextWorkspace);
    setWorkspaceBrowserOpen(false);
  }
  function handleModelChange(model: string) {
    sourcePrefilledRef.current = true;
    setPickedModel(model);
    if (
      pickedEffort &&
      !harnessSettings.effortsForModel(model).some((option) => option.value === pickedEffort)
    ) {
      setPickedEffort("");
    }
  }

  const workspaceHostOffline = !sandboxMode && selectedHost?.status === "offline";
  const workspacePickerVisible =
    open && workspaceBrowserOpen && !workspaceHostOffline && !sandboxMode && hostId !== "";
  useScheduleEscape(workspacePickerVisible, () => {
    setWorkspaceBrowserOpen(false);
    return true;
  });
  const { data: worktrees, isFetching: worktreesChecking } = useHostWorktrees(
    open && worktreesSupported && !sandboxMode && !workspaceHostOffline && hostId !== ""
      ? hostId
      : null,
    workspace || null,
  );
  const isGitWorkspace = (worktrees?.length ?? 0) > 0;
  let worktreeStatus = "The selected directory is not a Git repository.";
  if (workspaceHostOffline) {
    worktreeStatus = "Connect your host to verify this directory.";
  } else if (worktreesChecking) {
    worktreeStatus = "Checking whether this directory is a Git repository.";
  } else if (worktrees === undefined) {
    worktreeStatus = "Repository verification is unavailable.";
  }
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
    validateSchedule(schedule, { allowPastEnd, sessionMode, subhourSupported, executionTarget }) !==
      null;
  const renderedTemplatePrompt =
    trigger !== null && triggerFieldsComplete(triggerFields, trigger)
      ? renderTriggerPrompt(trigger, triggerFields, {
          // Workers run where the manager runs: beside it on a connected host,
          // or each in its own sandbox. A switched edit keeps the saved placement.
          placement: workerPlacement ?? (sandboxMode ? "managed" : "same_host"),
          sessionLink,
          pollingIntervalMinutes:
            schedule.preset === "custom" && schedule.customFreq === "minutely"
              ? schedule.interval
              : 60,
          // A switched edit keeps the saved workers' harness, model, effort, and mode.
          ...(workerSettings !== null
            ? {
                agentId: workerSettings.agentId,
                model: workerSettings.model || undefined,
                effort: workerSettings.effort || undefined,
                permissionMode: workerSettings.permissionMode || undefined,
              }
            : {
                model: supportsModel && pickedModel !== "" ? pickedModel : undefined,
                effort: supportsModel && pickedEffort !== "" ? pickedEffort : undefined,
                permissionMode: workerPermissionMode,
              }),
          taskSessionMode: triggerTaskSessionMode,
          taskSessionTitle: name,
        })
      : "";
  const effectivePrompt =
    trigger !== null ? renderedTemplatePrompt : combineTriggerPrompt(prompt, systemPrompt);
  const triggerMode =
    trigger !== null ||
    (sessionMode === "continue" && (editingTemplateTrigger || isTriggerPrompt(effectivePrompt)));
  const sendsToTaskSession =
    trigger !== null
      ? triggerTaskSessionMode === "continue"
      : isTaskSessionTriggerPrompt(editingTask?.prompt ?? effectivePrompt);
  const pollingBlockedReason =
    editingWorkerSettings && reusesExistingSession ? null : harnessSettings.blockedReason;
  const recurrenceValid =
    validateSchedule(
      { ...schedule, end: undefined },
      { sessionMode, subhourSupported, executionTarget },
    ) === null;
  // Collapsed fields must not hide an error that blocks submit.
  const endingInvalid =
    validateSchedule({ ...DEFAULT_SCHEDULE_MODEL, end: schedule.end }, { allowPastEnd }) !== null ||
    (!anchoredScheduling && schedule.end?.type === "count");
  const advancedError =
    workspaceWithoutHost ||
    Boolean(pollingBlockedReason) ||
    (workerSettings !== null && Boolean(workerHarnessSettings.blockedReason)) ||
    (!triggerMode && recurrenceValid && endingInvalid);
  useEffect(() => {
    if (open && advancedError) setAdvancedOpen(true);
  }, [open, advancedError]);
  const displayedModel = reusesExistingSession ? (editingTask?.modelOverride ?? "") : pickedModel;
  const displayedEffort = reusesExistingSession
    ? (editingTask?.reasoningEffort ?? "")
    : pickedEffort;
  const displayedPermission = reusesExistingSession
    ? editablePermissionMode(editingTask?.permissionMode)
    : effectivePermission;
  const summarySettings = editingWorkerSettings ? workerHarnessSettings : harnessSettings;
  const summaryModel = editingWorkerSettings ? (workerSettings?.model ?? "") : displayedModel;
  const summaryEffort = editingWorkerSettings ? (workerSettings?.effort ?? "") : displayedEffort;
  const summaryPermission = editingWorkerSettings ? "" : displayedPermission;
  const overrideSummary = [
    summarySettings.models.find((option) => option.id === summaryModel)?.displayName ??
      summaryModel,
    summarySettings.efforts.find((option) => option.value === summaryEffort)?.label ??
      summaryEffort,
    summarySettings.permissions.find((option) => option.value === summaryPermission)?.label ??
      summaryPermission,
    !triggerMode && schedule.end ? "End limit" : null,
    trigger !== null && triggerTaskSessionMode === "continue" ? "One task session" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  // Set across the whole submit, which may first find or create a project.
  const [submitting, setSubmitting] = useState(false);
  const mutationPending = submitting || createMutation.isPending || updateMutation.isPending;
  const canSubmit =
    !curationPending &&
    name.trim() !== "" &&
    (startsFromSession ||
      (effectivePrompt.trim() !== "" && (trigger !== null || prompt.trim() !== ""))) &&
    (trigger !== null || systemPrompt === null || systemPrompt.trim() !== "") &&
    (isEdit || effectiveAgentId !== null) &&
    (isEdit || sandboxMode || hostId !== "") &&
    !workspaceWithoutHost &&
    !scheduleInvalid &&
    !pollingBlockedReason &&
    !(workerSettings !== null && workerHarnessSettings.blockedReason) &&
    !mutationPending;

  function handleTriggerTypeChange(next: AutomationTriggerType) {
    if (isEdit || next === triggerType || (next !== "schedule" && !triggersSupported)) return;
    typeDrafts.current[triggerType] = {
      name,
      prompt,
      systemPrompt,
      trigger,
      triggerFields,
      schedule,
      timezone,
      sessionMode,
    };
    const previousDefaultName =
      triggerType === "schedule" ? (initialName ?? "") : TRIGGER_TEMPLATES[triggerType].name;
    const nextDefaultName =
      next === "schedule" ? (initialName ?? "") : TRIGGER_TEMPLATES[next].name;
    const customName = name.trim() !== "" && name !== previousDefaultName;
    const draft = typeDrafts.current[next];
    const nextPrompt = trigger !== null ? triggerFields.instructions : prompt;
    setTriggerType(next);
    setName(draft?.name ?? (customName ? name : nextDefaultName));
    setPrompt(draft?.prompt ?? nextPrompt);
    setSystemPrompt(draft?.systemPrompt ?? null);
    setTrigger(draft ? draft.trigger : next === "schedule" ? null : next);
    setTriggerFields(
      draft?.triggerFields ?? {
        ...emptyTriggerFields(next === "schedule" ? "jira" : next),
        instructions: nextPrompt,
      },
    );
    setSchedule(
      draft?.schedule ?? (next === "schedule" ? DEFAULT_SCHEDULE_MODEL : defaultTriggerSchedule),
    );
    setTimezone(draft?.timezone ?? timezone);
    setSessionMode(draft?.sessionMode ?? (next === "schedule" ? "new" : "continue"));
    setScheduleUnsupported(false);
    setSystemPromptOpen(false);
    setEditingSystemPrompt(false);
    setError(null);
  }

  // The source an edited trigger watches now: its open form, or its prompt's opening line.
  const editedTriggerSource =
    editingTriggerSource === null
      ? null
      : (trigger ?? sourceSwitch?.source ?? editingTriggerSource);
  // The polling session's state tracks the saved source's items, so a switch always resets it.
  const triggerSourceSwitched = sourceSwitch !== null;

  /** Edit only: watch another integration, or restore the saved prompt for the original one. */
  function handleEditTriggerSourceChange(next: TriggerSource) {
    if (editingTriggerSource === null || editedTriggerSource === null) return;
    if (next === editedTriggerSource) return;
    const saved = sourceSwitch ?? {
      source: editingTriggerSource,
      savedPrompt: prompt,
      savedSystemPrompt: systemPrompt,
      savedStartNewSession: startNewSession,
    };
    if (sourceSwitch !== null) {
      editSourceDrafts.current[editedTriggerSource] = {
        fields: triggerFields,
        promptEditor: trigger === null ? { prompt, systemPrompt } : null,
      };
    }
    // A split template prompt's user part is the instructions for each session.
    let instructions = "";
    if (trigger !== null) instructions = triggerFields.instructions;
    else if (systemPrompt !== null) instructions = prompt;
    if (name === TRIGGER_TEMPLATES[editedTriggerSource].name) {
      setName(TRIGGER_TEMPLATES[next].name);
    }
    // Restored prompts and drafts keep their text, with the current worker settings;
    // only a new draft takes the instructions.
    const draft = next === editingTriggerSource ? null : editSourceDrafts.current[next];
    const promptEditor =
      next === editingTriggerSource
        ? { prompt: saved.savedPrompt, systemPrompt: saved.savedSystemPrompt }
        : draft?.promptEditor;
    if (promptEditor) {
      const restored = restoreSourcePrompt(
        promptEditor.prompt,
        promptEditor.systemPrompt,
        workerSettings,
      );
      setPrompt(restored.userPrompt);
      setSystemPrompt(restored.systemPrompt);
      setTrigger(null);
    } else {
      setTrigger(next);
    }
    // Fall back to the saved workers if the prompt editor's creation call no longer parses.
    setSwitchedWorkerSettings(workerSettings ?? savedWorkerSettings);
    setSwitchedWorkerPlacement(workerSettings !== null ? workerPlacement : savedWorkerPlacement);
    if (next === editingTriggerSource) {
      setStartNewSession(saved.savedStartNewSession);
      setSourceSwitch(null);
    } else {
      setTriggerFields(draft?.fields ?? { ...emptyTriggerFields(next), instructions });
      setStartNewSession(true);
      setSourceSwitch({ ...saved, source: next });
    }
    setSystemPromptOpen(false);
    setEditingSystemPrompt(false);
    setError(null);
  }

  function resetForm() {
    setName("");
    setPrompt("");
    setSystemPrompt(null);
    setPickedAgentId(null);
    setPickedModel("");
    setPickedEffort("");
    setPickedPermission("");
    setPermissionPicked(false);
    setSchedule(DEFAULT_SCHEDULE_MODEL);
    setHostId("");
    setWorkspace("");
    resetMenus();
    setSandboxMode(false);
    setProjectChoice(null);
    setPickedProjectId(null);
    setTrigger(null);
    setTriggerFields(emptyTriggerFields("jira"));
    setTriggerTaskSessionMode("new");
    setError(null);
    setScheduleUnsupported(false);
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetForm();
    onOpenChange(next);
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    if (!editingTask && effectiveAgentId === null) return;
    setError(null);
    setSubmitting(true);
    try {
      // Create sends non-default target/repository; edit sends them only when changed.
      const executionTargetChanged = editingTask
        ? editingTask.executionTarget !== executionTarget
        : sandboxMode;
      const selectedWorkspace = workspace.trim();
      const workspaceChanged = editingTask !== null && selectedWorkspace !== savedWorkspace;
      let workspacePatch: { workspace?: string | null } = {};
      if (sandboxMode) {
        const hasNewSelection = editingTask === null && selectedWorkspace !== "";
        if (sandboxRepoCatalog.length > 0 && (workspaceChanged || hasNewSelection)) {
          workspacePatch = { workspace: selectedWorkspace || null };
        }
      } else if (hostId !== "" && selectedWorkspace !== "") {
        workspacePatch = { workspace: selectedWorkspace };
      }
      const input = {
        name: name.trim(),
        prompt: systemPrompt === null ? effectivePrompt.trim() : effectivePrompt,
        rrule: buildRRule(schedule),
        timezone,
        ...(executionTargetChanged ? { executionTarget } : {}),
        ...(worktreesSupported && baseBranch !== (editingTask?.baseBranch ?? null)
          ? { baseBranch }
          : {}),
        ...(!sandboxMode && hostId !== "" ? { hostId } : {}),
        ...workspacePatch,
        ...((editingTask?.sessionMode ?? "new") !== sessionMode ? { sessionMode } : {}),
      };
      const projectInput = await projectChangeInput(
        // Saving over a deleted project clears it, matching the "No project" shown.
        effectiveProjectChoice ?? (savedProjectDeleted ? "" : null),
        pickedProjectId,
        savedProjectId,
        resolveProjectId,
      );
      if (editingTask) {
        // Continued-session edits omit launch settings, except to clear a stored mode
        // the server refuses; new targets clear stale overrides.
        const overrides = reusesExistingSession
          ? editablePermissionMode(editingTask.permissionMode) !==
            (editingTask.permissionMode ?? "")
            ? { permissionMode: null }
            : {}
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
            ...(sessionMode === "continue" &&
            (startNewSession || triggerSourceSwitched) &&
            editingTask.sessionId !== null
              ? { sessionId: null }
              : {}),
            // Only on a real switch: sending the unchanged agent is a server-side
            // no-op, but omitting it keeps the PATCH honest about what changed.
            ...(agentChanged && effectiveAgentId !== null ? { agentId: effectiveAgentId } : {}),
          },
        });
      } else {
        if (effectiveAgentId === null) return;
        const scheduleTemplateId = initialTrigger ? undefined : initialTemplateId;
        const templateId = triggerType === "schedule" ? scheduleTemplateId : triggerType;
        const created = await createMutation.mutateAsync({
          ...(templateId ? { templateId } : {}),
          ...input,
          ...projectInput,
          ...(fromSession ? { sourceSessionId: fromSession.id } : {}),
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
        onCreated?.(created);
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
        className="flex max-h-[90vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-[560px]"
        data-testid="create-scheduled-task-dialog"
        onOpenAutoFocus={(event) => {
          const instructions = document.getElementById("task-prompt");
          if (instructions) {
            event.preventDefault();
            instructions.focus();
          }
        }}
        // Keep a nested Select's dismiss (pick an option, OR click empty modal
        // body / trigger while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss` — it covers both the popper-target path and the
        // Select-open + focus-outside path, while leaving real backdrop clicks
        // and Escape to close as normal.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>
            {fromSession
              ? `New automation from “${fromSession.title || "this session"}”`
              : isEdit
                ? "Edit automation"
                : "New automation"}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {triggerMode
              ? "Starts an agent session for each matching item."
              : isEdit
                ? "Update this recurring agent session."
                : "Runs an agent session on a recurring schedule."}
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-5"
          data-testid="scheduled-task-dialog-body"
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-name">Name</Label>
            <Input
              id="task-name"
              value={name}
              placeholder="Name this automation"
              data-testid="task-name-input"
              className="text-ui focus-visible:ring-0"
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          {startsFromSessionId !== null && (
            <StartsFromField
              sessionId={startsFromSessionId}
              title={fromSession ? fromSession.title : null}
              note={
                fromSession
                  ? "Each run starts with a copy of this session's history as of now."
                  : "Each run starts with a copy of this session's history."
              }
              onNavigate={() => handleOpenChange(false)}
              onSession={fromSession ? setSourceSnapshot : undefined}
            />
          )}
          {!isEdit && !fromSession && (
            <TriggerTypeSelect
              value={triggerType}
              types={AUTOMATION_TRIGGER_TYPES}
              isTypeDisabled={(type) => type !== "schedule" && !triggersSupported}
              onChange={handleTriggerTypeChange}
              onOpenChange={handleSelectOpenChange}
              disabled={curationPending || mutationPending}
            >
              {!curationPending && !triggersSupported && (
                <p className="text-sm text-muted-foreground">
                  Integration triggers aren't available on this server.
                </p>
              )}
            </TriggerTypeSelect>
          )}
          {editingTriggerSource !== null && editedTriggerSource !== null && triggersSupported && (
            <TriggerTypeSelect
              value={editedTriggerSource}
              types={TRIGGER_SOURCES}
              onChange={handleEditTriggerSourceChange}
              onOpenChange={handleSelectOpenChange}
              disabled={curationPending || mutationPending}
            >
              {triggerSourceSwitched && (
                <p className="text-sm text-muted-foreground" data-testid="task-trigger-type-note">
                  Saving replaces the {triggerServiceName(editingTriggerSource)} trigger. The next
                  run starts a new polling session that watches{" "}
                  {triggerServiceName(editedTriggerSource)}.
                  {sendsToTaskSession && " Its first match starts a new task session."}
                </p>
              )}
            </TriggerTypeSelect>
          )}
          <ScheduleFields
            timezone={timezone}
            onTimezoneChange={setTimezone}
            model={schedule}
            anchoredScheduling={anchoredScheduling}
            sessionMode={sessionMode}
            executionTarget={executionTarget}
            subhourSupported={subhourSupported}
            recurrenceUnsupported={scheduleUnsupported}
            allowPastEnd={allowPastEnd}
            onChange={setSchedule}
            onRecurrenceSelect={() => setScheduleUnsupported(false)}
            onSelectOpenChange={handleSelectOpenChange}
          >
            {({ recurrenceFields, endingFields }) => (
              <>
                {trigger !== null ? (
                  <TriggerTemplateFields
                    source={trigger}
                    fields={triggerFields}
                    onChange={setTriggerFields}
                    onSelectOpenChange={handleSelectOpenChange}
                    // Arclet sandboxes provide their agents' MCP tools, Jira and Slack included.
                    toolsProvided={
                      sandboxMode && info !== "loading" && info.sandbox_provider === "arclet"
                    }
                    taskSessionMode={triggerTaskSessionMode}
                    renderedPrompt={renderedTemplatePrompt}
                    onEditAsPrompt={() => {
                      const parts = splitTriggerPrompt(renderedTemplatePrompt);
                      setPrompt(parts.userPrompt);
                      setSystemPrompt(parts.systemPrompt);
                      setSystemPromptOpen(false);
                      setEditingSystemPrompt(false);
                      setTrigger(null);
                    }}
                  />
                ) : (
                  <div ref={promptFieldsRef} className="flex flex-col gap-1.5">
                    <Label htmlFor="task-prompt">
                      {startsFromSession
                        ? "Additional instructions"
                        : systemPrompt === null
                          ? "Instructions"
                          : "Your prompt"}
                    </Label>
                    <Textarea
                      id="task-prompt"
                      value={prompt}
                      rows={5}
                      placeholder={
                        startsFromSession
                          ? "Optional. For example: Use this week's data."
                          : "What should the agent do each run?"
                      }
                      data-testid="task-prompt-input"
                      componentId="tasks.scheduled.prompt"
                      className="min-h-32 resize-y rounded-xl p-4 text-ui focus-visible:ring-0"
                      onChange={(e) => setPrompt(e.target.value)}
                    />
                    {systemPrompt !== null &&
                      splitTriggerPrompt(effectivePrompt).systemPrompt === null && (
                        <p role="status" className="text-sm text-warning">
                          {prompt.trim() === ""
                            ? "Your prompt can't be empty."
                            : systemPrompt.trim() === ""
                              ? "System prompt can't be empty. Expand System prompt and click Edit system prompt to restore it."
                              : "These edits change the template structure. After saving, the full text will appear as one prompt."}
                        </p>
                      )}
                    {systemPrompt !== null && (
                      <Collapsible
                        open={systemPromptOpen}
                        onOpenChange={(expanded) => {
                          setSystemPromptOpen(expanded);
                          setEditingSystemPrompt(false);
                        }}
                        className="mt-2 rounded-md border border-border p-3"
                      >
                        <CollapsibleTrigger className="group flex w-full cursor-pointer items-center gap-2 rounded-sm text-ui text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                          <ChevronDownIcon className="size-4 transition-transform group-data-[state=closed]:-rotate-90" />
                          System prompt
                        </CollapsibleTrigger>
                        <CollapsibleContent className="space-y-2 pt-3">
                          <p className="text-sm text-muted-foreground">
                            Instructions for how the automation runs. Your prompt is added
                            separately.
                          </p>
                          {editingSystemPrompt ? (
                            <Textarea
                              aria-label="System prompt"
                              autoFocus
                              value={systemPrompt}
                              rows={8}
                              className="max-h-80 resize-y text-ui focus-visible:ring-0"
                              componentId="tasks.scheduled.system_prompt"
                              onChange={(event) => setSystemPrompt(event.target.value)}
                            />
                          ) : (
                            <pre className="max-h-56 overflow-y-auto rounded-md bg-muted/40 p-2 font-sans text-sm whitespace-pre-wrap break-words">
                              {systemPrompt}
                            </pre>
                          )}
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            componentId="tasks.scheduled.edit_system_prompt"
                            onClick={() => setEditingSystemPrompt(!editingSystemPrompt)}
                          >
                            {editingSystemPrompt
                              ? "Done editing system prompt"
                              : "Edit system prompt"}
                          </Button>
                        </CollapsibleContent>
                      </Collapsible>
                    )}
                  </div>
                )}

                <div className="space-y-3 rounded-xl border border-border px-4 py-3">
                  {/* Pin a connected host, or provision a sandbox per run (kept for continuing sessions). */}
                  <div
                    className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2 [&>p]:col-span-2"
                    data-testid="task-host-field"
                  >
                    <Label htmlFor="task-host">Run on</Label>
                    <Select
                      value={sandboxMode ? SANDBOX_HOST : hostId}
                      componentId="tasks.scheduled.host"
                      onValueChange={handleTargetChange}
                      onOpenChange={handleSelectOpenChange}
                    >
                      <SelectTrigger
                        id="task-host"
                        data-testid="task-host-trigger"
                        className={cn(
                          "w-auto min-w-0 max-w-full justify-self-end justify-end border-0 shadow-none text-muted-foreground",
                          pickerTriggerClass,
                        )}
                      >
                        <SelectValue placeholder={isEdit ? UNPINNED_HOST_LABEL : "Pick a host"} />
                      </SelectTrigger>
                      <SelectContent position="popper" align="end">
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
                    {!sandboxMode && selectedHost?.status === "offline" && (
                      <p className="text-sm text-muted-foreground">
                        This host is offline. Runs fail until it reconnects.
                      </p>
                    )}
                    {sandboxMode && sandboxAutomationNotice && (
                      <p
                        className="text-sm text-muted-foreground"
                        data-testid="task-sandbox-automation-notice"
                      >
                        {sandboxAutomationNotice}
                      </p>
                    )}
                  </div>

                  {editingWorkerSettings ? (
                    workerSettings !== null &&
                    editingTask && (
                      <div className="border-t border-border pt-3">
                        <TriggerWorkerHarnessField
                          value={workerSettings}
                          pollingAgentId={editingTask.agentId}
                          agents={agentList}
                          selectedAgent={workerAgent}
                          disabled={curationPending}
                          onSelectOpenChange={handleSelectOpenChange}
                          onChange={handleWorkerSettingsChange}
                        />
                      </div>
                    )
                  ) : (
                    <div
                      className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2 border-t border-border pt-3 [&>p]:col-span-2"
                      data-testid="task-agent-field"
                    >
                      <Label>Agent</Label>
                      <div className="min-w-0 justify-self-end" data-testid="task-agent-picker">
                        <AgentHarnessPicker
                          agentEntries={agentEntries}
                          harnessEntries={harnessEntries}
                          effectiveAgentId={effectiveAgentId}
                          agentLabel={agentLabel}
                          hasAgents={selectedAgent !== undefined || agentList.length > 0}
                          loading={curationPending}
                          curatedEntries={
                            curation.restrictToAgentNames.size > 0 ? agentList : undefined
                          }
                          // Sandboxes provision their own harnesses; host badges only apply to hosts.
                          host={sandboxMode ? undefined : badgeHost}
                          onSelectAgent={handleSelectAgent}
                          pendingAgent={null}
                          pendingAgentId="__unused_pending_agent__"
                          onSelectPending={() => {}}
                          // Custom-agent creation is inert until there is a way to
                          // persist a new agent independently of creating a session.
                          onCreateCustomAgent={() => {}}
                          sandboxSelected={sandboxMode}
                          allowCreateCustomAgent={false}
                          open={agentMenuOpen}
                          onOpenChange={changeAgentMenuOpen}
                          // This picker is nested inside a Dialog. Radix DropdownMenu's
                          // default modal mode can turn an inside-dialog click into a
                          // parent Dialog outside interaction while the menu dismisses.
                          dropdownModal={false}
                          // The shared menu caps against available viewport height, so
                          // constrained screens scroll without clipping taller screens.
                          contentClassName="w-80"
                          contentAlign="end"
                          triggerClassName={cn(
                            "h-8 max-w-full justify-end gap-1.5 rounded-lg border-0 bg-transparent pr-2 pl-2.5 text-muted-foreground shadow-none",
                            pickerTriggerClass,
                          )}
                          triggerLabelClassName="min-w-0 max-w-none text-ui [&>span]:text-ui [&>span]:font-normal [&>span]:text-inherit"
                        />
                      </div>
                      {agentChanged && (
                        <p className="text-sm text-muted-foreground">
                          Future runs use {agentLabel}; past runs keep the agent they ran with
                        </p>
                      )}
                    </div>
                  )}

                  {/* A trigger needs its one kept session, so its form leaves the choice out. */}
                  {sessionModesSupported &&
                    trigger === null &&
                    !startsFromSession &&
                    (!editingTemplateTrigger || editingTask?.sessionId) && (
                      <div
                        className="flex flex-col gap-2 border-t border-border pt-3"
                        data-testid="task-session-mode-field"
                      >
                        {!editingTemplateTrigger && (
                          <>
                            <div className="flex items-center justify-between gap-4">
                              <Label htmlFor="task-session-mode">
                                Start each run in a new session
                              </Label>
                              <Switch
                                id="task-session-mode"
                                checked={sessionMode === "new"}
                                onCheckedChange={(checked) =>
                                  setSessionMode(checked ? "new" : "continue")
                                }
                                data-testid="task-session-mode-trigger"
                                componentId="tasks.scheduled.session_mode"
                              />
                            </div>
                            {sessionMode === "continue" && (
                              <p className="text-sm text-muted-foreground">
                                Later runs continue in the same session, keeping its context.
                              </p>
                            )}
                          </>
                        )}
                        {isEdit && sessionMode === "continue" && editingTask?.sessionId && (
                          <div className="flex items-center gap-2">
                            <Checkbox
                              id="task-start-new-session"
                              checked={startNewSession || triggerSourceSwitched}
                              disabled={triggerSourceSwitched}
                              onCheckedChange={(checked) => setStartNewSession(checked === true)}
                              data-testid="task-start-new-session"
                              componentId="tasks.scheduled.start_new_session"
                            />
                            <label
                              htmlFor="task-start-new-session"
                              className="cursor-pointer text-sm"
                            >
                              Start a new session on the next run
                            </label>
                          </div>
                        )}
                      </div>
                    )}
                </div>
                {!triggerMode && recurrenceFields}
                {triggerMode && !scheduleInvalid && (
                  <p className="text-sm text-muted-foreground" data-testid="task-trigger-cadence">
                    Checks for new matches: {describeSchedule(buildRRule(schedule))}.{" "}
                    {sendsToTaskSession
                      ? "Matching items go to the task session automatically."
                      : "Matching items start sessions automatically."}
                  </p>
                )}
                {triggerMode && scheduleInvalid && (
                  <div className="space-y-2">
                    <p className="text-sm text-destructive" role="alert">
                      This polling configuration is no longer supported. Use the server default to
                      save your changes.
                    </p>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setSchedule(defaultTriggerSchedule);
                        setScheduleUnsupported(false);
                      }}
                    >
                      Use server default ({describeSchedule(buildRRule(defaultTriggerSchedule))})
                    </Button>
                  </div>
                )}
                {!isEdit &&
                  !curationPending &&
                  agents !== undefined &&
                  effectiveAgentId === null && (
                    <p role="alert" className="text-sm text-destructive">
                      No agents are available for this automation.
                    </p>
                  )}
                {!triggerMode && scheduleUnsupported && (
                  <p className="text-sm text-destructive" role="alert">
                    This schedule can&apos;t be edited in this form yet. Choose a frequency to
                    replace it.
                  </p>
                )}

                {!triggerMode && unsupportedByScheduler && (
                  <p className="text-sm text-destructive" role="alert">
                    This server does not support custom schedules or occurrence limits. Choose a
                    supported frequency and ending to replace this schedule.
                  </p>
                )}

                {!advancedOpen && advancedError && (
                  <p role="alert" className="text-sm text-destructive">
                    Expand Advanced to fix settings before saving.
                  </p>
                )}
                <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
                  <CollapsibleTrigger className="group/advanced flex w-full cursor-pointer items-center gap-2 rounded-sm py-1 text-ui text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                    <span>Advanced</span>
                    <ChevronDownIcon className="size-4 shrink-0 transition-transform group-data-[state=open]/advanced:rotate-180" />
                    <span className="ml-auto truncate text-xs" aria-hidden="true">
                      {overrideSummary}
                    </span>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="space-y-4 pt-4" data-testid="task-advanced-fields">
                    {trigger !== null && editingTask === null && (
                      <div
                        className="flex flex-col gap-2 rounded-xl border border-border p-4"
                        data-testid="task-trigger-session-mode-field"
                      >
                        <div className="flex items-center justify-between gap-4">
                          <Label htmlFor="task-trigger-session-mode">
                            Start a new session for each match
                          </Label>
                          <Switch
                            id="task-trigger-session-mode"
                            checked={triggerTaskSessionMode === "new"}
                            onCheckedChange={(checked) =>
                              setTriggerTaskSessionMode(checked ? "new" : "continue")
                            }
                            data-testid="task-trigger-session-mode"
                            componentId="tasks.scheduled.trigger_session_mode"
                          />
                        </div>
                        {triggerTaskSessionMode === "continue" && (
                          <p className="text-sm text-muted-foreground">
                            Every match goes to one task session, started on the first match, so it
                            keeps its context across matches. Archiving it starts a new one on the
                            next match.
                          </p>
                        )}
                      </div>
                    )}
                    <div className="space-y-4 rounded-xl border border-border p-4">
                      {sandboxMode && sandboxRepoCatalog.length > 0 && (
                        <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2 [&>p]:col-span-2">
                          <Label htmlFor="task-sandbox-repository">Repository</Label>
                          <Select
                            value={sandboxWorkspace || PROVIDER_DEFAULT}
                            onValueChange={(value) =>
                              setWorkspace(value === PROVIDER_DEFAULT ? "" : value)
                            }
                            onOpenChange={handleSelectOpenChange}
                            componentId="tasks.scheduled.sandbox_repository"
                          >
                            <SelectTrigger
                              id="task-sandbox-repository"
                              className={cn(
                                "w-full border-0 shadow-none text-muted-foreground",
                                pickerTriggerClass,
                              )}
                            >
                              <SelectValue placeholder="Provider default" />
                            </SelectTrigger>
                            <SelectContent position="popper" align="start">
                              <SelectItem value={PROVIDER_DEFAULT}>Provider default</SelectItem>
                              {savedSandboxWorkspace &&
                                !sandboxRepoCatalog.some(
                                  (repo) => repo.url === savedSandboxWorkspace,
                                ) && (
                                  <SelectItem value={savedSandboxWorkspace}>
                                    {savedSandboxWorkspace}
                                  </SelectItem>
                                )}
                              {sandboxRepoCatalog.map((repo) => (
                                <SelectItem key={repo.url} value={repo.url}>
                                  {repo.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      )}

                      {!sandboxMode && hostId !== "" && (
                        <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2 [&>p]:col-span-2">
                          <Label>Workspace (optional)</Label>
                          <Button
                            type="button"
                            variant="outline"
                            className={cn(
                              "w-full justify-start gap-2 border-0 shadow-none text-muted-foreground",
                              pickerTriggerClass,
                            )}
                            disabled={workspaceHostOffline}
                            aria-describedby={
                              workspaceHostOffline ? "task-workspace-help" : undefined
                            }
                            onClick={() => setWorkspaceBrowserOpen(true)}
                            data-testid="task-workspace-browse"
                          >
                            <FolderOpenIcon className="size-4 text-muted-foreground" />
                            <span className="truncate">{workspace || "Browse folders…"}</span>
                          </Button>
                          {workspaceHostOffline && (
                            <p id="task-workspace-help" className="text-sm text-muted-foreground">
                              Connect your host to select a workspace.
                            </p>
                          )}
                          <WorkspacePickerDialog
                            open={workspacePickerVisible}
                            onOpenChange={setWorkspaceBrowserOpen}
                            hostId={hostId}
                            initialPath={workspace}
                            onConfirm={(path) => {
                              if (path !== workspace) setBaseBranch(null);
                              setWorkspace(path);
                            }}
                          />
                          {workspace && (
                            <>
                              <p className="truncate font-mono text-sm text-muted-foreground">
                                {workspace}
                              </p>
                              {worktreesSupported && !isGitWorkspace && baseBranch !== null && (
                                <div className="col-span-2 mt-2 text-sm text-muted-foreground">
                                  <p>Random worktree is enabled. {worktreeStatus}</p>
                                  <Button
                                    type="button"
                                    variant="link"
                                    onClick={() => setBaseBranch(null)}
                                  >
                                    Turn off random worktree
                                  </Button>
                                </div>
                              )}
                              {worktreesSupported && isGitWorkspace && (
                                <>
                                  <div className="col-span-2 mt-2 flex items-center gap-2">
                                    <Checkbox
                                      id="task-random-worktree"
                                      checked={baseBranch !== null}
                                      onCheckedChange={(checked) =>
                                        setBaseBranch(
                                          checked === true ? RANDOM_WORKTREE_BASE_REF : null,
                                        )
                                      }
                                      componentId="tasks.scheduled.random_worktree"
                                    />
                                    <label
                                      htmlFor="task-random-worktree"
                                      className="cursor-pointer text-sm"
                                    >
                                      Random worktree
                                    </label>
                                  </div>
                                  <p className="text-sm text-muted-foreground">
                                    {sessionMode === "continue"
                                      ? "Creates a randomly named worktree from the repository’s current HEAD when starting a session. Later runs reuse it."
                                      : "Creates a fresh randomly named worktree from the repository’s current HEAD for each run."}
                                  </p>
                                </>
                              )}
                            </>
                          )}
                        </div>
                      )}
                      {projectsSupported && (
                        <div
                          className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-2 [&>p]:col-span-2"
                          data-testid="task-project-field"
                        >
                          <Label>Project (optional)</Label>
                          <DropdownMenu open={projectMenuOpen} onOpenChange={changeProjectMenuOpen}>
                            <DropdownMenuTrigger asChild>
                              <Button
                                type="button"
                                variant="outline"
                                className={cn(
                                  "w-full justify-start gap-2 border-0 shadow-none text-muted-foreground",
                                  pickerTriggerClass,
                                )}
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
                                  setPickedProjectId(
                                    projects.find((p) => p.name === next)?.id ?? null,
                                  );
                                }}
                              />
                            </DropdownMenuContent>
                          </DropdownMenu>
                          <p className="text-sm text-muted-foreground">
                            Type a new name to create a project.
                          </p>
                        </div>
                      )}
                    </div>
                    <div className="space-y-4 rounded-xl border border-border p-4">
                      {workspaceWithoutHost && (
                        <p
                          className="flex items-center gap-1.5 text-sm text-destructive"
                          data-testid="workspace-without-host-error"
                        >
                          <TriangleAlertIcon className="size-3.5 shrink-0" />
                          Pick a host before pinning a workspace.
                        </p>
                      )}

                      {editingWorkerSettings && pollingBlockedReason && (
                        <p role="alert" className="text-sm text-destructive">
                          Polling session cannot start: {pollingBlockedReason}
                        </p>
                      )}

                      {editingWorkerSettings && workerSettings !== null && editingTask ? (
                        <TriggerWorkerSettingsFields
                          value={workerSettings}
                          settings={workerHarnessSettings}
                          permissionModesSupported={sessionPermissionModesSupported}
                          taskSession={sendsToTaskSession}
                          disabled={curationPending}
                          onSelectOpenChange={handleSelectOpenChange}
                          onChange={handleWorkerSettingsChange}
                        />
                      ) : editingWorkerSettings ? (
                        <p className="text-sm text-muted-foreground" role="status">
                          Worker controls are unavailable for these customized instructions. Edit
                          the prompt or system prompt above to configure matching sessions.
                        </p>
                      ) : (
                        <>
                          {!showAdvanced && (
                            <p className="text-sm text-muted-foreground">
                              Uses this agent&apos;s default model, effort, and permission settings
                            </p>
                          )}

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
                            <div data-testid="task-model-effort-field">
                              <ModelEffortFields
                                model={displayedModel}
                                effort={displayedEffort}
                                permissionMode={displayedPermission}
                                settings={harnessSettings}
                                disabled={curationPending || reusesExistingSession}
                                onModelChange={handleModelChange}
                                onEffortChange={(effort) => {
                                  sourcePrefilledRef.current = true;
                                  setPickedEffort(effort);
                                }}
                                onPermissionModeChange={(mode) => {
                                  sourcePrefilledRef.current = true;
                                  setPickedPermission(mode);
                                  setPermissionPicked(true);
                                }}
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
                                    This automation continues in an existing session, so its model,
                                    effort, and permission mode are controlled by that session. Edit
                                    the session directly to change them.
                                    {editingTemplateTrigger && (
                                      <>
                                        {" "}
                                        {sendsToTaskSession
                                          ? "Trigger automations reuse a polling session, which sends each match to its task session."
                                          : "Trigger automations reuse a polling session even though each match starts a new worker."}{" "}
                                        To change the model or harness for future workers, edit the
                                        worker creation instructions in the{" "}
                                        {systemPrompt === null ? "prompt" : "system prompt"}.{" "}
                                        {sendsToTaskSession &&
                                          "The current task session keeps its settings; archive it so the next match starts one with the new settings. "}
                                        <button
                                          type="button"
                                          className="underline underline-offset-2"
                                          onClick={() => {
                                            if (systemPrompt !== null) {
                                              setSystemPromptOpen(true);
                                              setEditingSystemPrompt(true);
                                            }
                                            promptFieldsRef.current
                                              ?.querySelector<HTMLTextAreaElement>(
                                                systemPrompt === null
                                                  ? "#task-prompt"
                                                  : 'textarea[aria-label="System prompt"]',
                                              )
                                              ?.focus();
                                          }}
                                        >
                                          Edit worker instructions
                                        </button>
                                      </>
                                    )}
                                  </p>
                                </div>
                              ) : (
                                <p className="mt-1.5 text-sm text-muted-foreground">
                                  Modes that require approval may pause a run until you respond.
                                </p>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                    {!triggerMode && endingFields}
                  </CollapsibleContent>
                </Collapsible>
              </>
            )}
          </ScheduleFields>
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
            {isEdit ? "Save changes" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The session runs start from, linked. Reports the session's snapshot so the
 * dialog can start from its launch settings. Mounted only for a session-based
 * automation, so other dialogs need no session query.
 */
function StartsFromField({
  sessionId,
  title,
  note,
  onNavigate,
  onSession,
}: {
  sessionId: string;
  /** Known title; `null` reads it from the session snapshot. */
  title: string | null;
  note: string;
  onNavigate: () => void;
  onSession?: (session: Session) => void;
}) {
  const { session } = useSession(sessionId);
  useEffect(() => {
    if (session) onSession?.(session);
  }, [session, onSession]);
  const label = title || session?.title || "Untitled session";
  return (
    <div className="flex flex-col gap-1.5" data-testid="task-starts-from">
      <Label>Starts from</Label>
      <Link
        to={`/c/${encodeURIComponent(sessionId)}`}
        onClick={onNavigate}
        className="min-w-0 truncate text-ui text-primary underline underline-offset-2 hover:text-primary/80"
        data-testid="task-starts-from-link"
      >
        {label}
      </Link>
      <p className="text-sm text-muted-foreground">{note}</p>
    </div>
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

const TRIGGER_SCHEDULE_MODEL: ScheduleModel = {
  ...DEFAULT_SCHEDULE_MODEL,
  preset: "custom",
  customFreq: "minutely",
  interval: 10,
};

const HOURLY_SCHEDULE_MODEL: ScheduleModel = { ...DEFAULT_SCHEDULE_MODEL, preset: "hourly" };

/** How an older task with no pinned host reads; the task detail page uses the same words. */
const UNPINNED_HOST_LABEL = "Most recently active host";

/** Sentinel Select value for the "New Sandbox" option (execution_target=managed_sandbox). */
const SANDBOX_HOST = "__sandbox__";

// The nested-dropdown dismiss guard now lives in a dependency-free module so
// other dialogs (project settings) can reuse it without pulling this file's
// module graph. Re-exported here to keep existing imports and tests stable.
export { isBackdropOverlay, isInsidePopper, shouldGuardDialogDismiss };
