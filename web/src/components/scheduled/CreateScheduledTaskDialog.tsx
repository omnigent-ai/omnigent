// Dialog for creating or editing a scheduled task. Reuses the existing agent,
// host, and workspace pickers where the backend can persist those fields.

import { useEffect, useMemo, useRef, useState } from "react";
import { TriangleAlertIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
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
import { ModelEffortFields } from "@/components/scheduled/ModelEffortFields";
import { WorkspacePicker } from "@/shell/WorkspacePicker";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useCreateScheduledTask, useUpdateScheduledTask } from "@/hooks/useScheduledTasks";
import { isNativeCodingAgent, nativeAgentHasCapability } from "@/lib/nativeCodingAgents";
import { sortAgentsForDisplay } from "@/lib/agentGrouping";
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
import { ScheduledTaskApiError, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { localTimezone } from "@/lib/timezones";

// Agents hidden from the scheduled-task picker (mirrors NewChatDialog's set):
// superseded / SDK-only harnesses that shouldn't be user-pickable here.
const HIDDEN_PICKER_AGENTS = new Set(["nessie", "kimi", "kimi-code"]);

export function CreateScheduledTaskDialog({
  open,
  onOpenChange,
  initialName,
  initialPrompt,
  editingTask = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Seed values applied to Name/Prompt when the dialog opens (e.g. from a
   *  "Suggestions" suggestion chip). Omitted → the fields start empty. */
  initialName?: string;
  initialPrompt?: string;
  editingTask?: ScheduledTask | null;
}) {
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts } = useHosts({ enabled: open });
  const createMutation = useCreateScheduledTask();
  const updateMutation = useUpdateScheduledTask();
  const isEdit = editingTask !== null;

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [schedule, setSchedule] = useState<ScheduleModel>(DEFAULT_SCHEDULE_MODEL);

  // ── Agent / harness picker (shared with NewChatDialog) ─────────────────────
  // The picker's "Harnesses" (Claude Code / Codex / Pi …) and "Agents"
  // (Polly / Debby / custom) rows are all real AvailableAgents with ids, so a
  // single `pickedAgentId` covers both cases — for a bare-harness pick it's the
  // `*-native-ui` agent's id, exactly what the interactive dialog sends.
  //
  // Scheduled tasks create sessions from the selected agent. Model + effort are
  // offered for native coding agents that support them (see `showModelEffort`
  // below), reusing lightweight scheduled-local pickers rather than the
  // interactive dialog's 26-prop HarnessConfigModal (bound to smart-routing /
  // cost-control / per-turn model loading — disproportionate for a saved task).
  // "" = unselected → `model_override` / `reasoning_effort` / `permission_mode`
  // are omitted so the fire path uses the agent's configured defaults. Permission
  // mode is offered for native coding agents that support it (Claude Code); each
  // fire launches a fresh session, so the whole launch vocabulary is valid —
  // including the launch-only `dontAsk` / `bypassPermissions`.
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [pickedModel, setPickedModel] = useState<string>("");
  const [pickedEffort, setPickedEffort] = useState<string>("");
  const [pickedPermission, setPickedPermission] = useState<string>("");

  const agentList = useMemo(
    () => sortAgentsForDisplay((agents ?? []).filter((a) => !HIDDEN_PICKER_AGENTS.has(a.name))),
    [agents],
  );
  const harnessEntries = useMemo(
    () => agentList.filter((a) => isNativeCodingAgent(a)),
    [agentList],
  );
  const agentEntries = useMemo(() => agentList.filter((a) => !isNativeCodingAgent(a)), [agentList]);
  // Resolve the effective selection: the explicit pick if it's still in the
  // list, else the first agent (so the picker always has a concrete value).
  const effectiveAgentId =
    (agentList.some((a) => a.id === pickedAgentId) ? pickedAgentId : agentList[0]?.id) ?? null;
  const selectedAgent = agentList.find((a) => a.id === effectiveAgentId);
  const agentLabel = selectedAgent
    ? selectedAgent.display_name
    : isEdit && editingTask
      ? editingTask.agentId
      : "Select agent";

  function handleSelectAgent(agent: AvailableAgent) {
    setPickedAgentId(agent.id);
  }

  // Model + effort are surfaced only for native coding agents that carry the
  // model/effort surface — the same `permissionMode` capability the interactive
  // dialog gates its Model/Effort/Permissions block on (Claude Code). Agents
  // without it (plain SDK agents like Polly, or native harnesses with no
  // model-picker surface) show no model/effort controls, exactly like
  // interactive. In edit mode the agent is read-only, so gate on the loaded
  // task's agent.
  const modelEffortAgent = isEdit
    ? agents?.find((a) => a.id === editingTask?.agentId)
    : selectedAgent;
  const showModelEffort = nativeAgentHasCapability(modelEffortAgent, "permissionMode");

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
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: selectOpenCountRef.current > 0,
        msSinceSelectClose: Date.now() - selectClosedAtRef.current,
      })
    ) {
      event.preventDefault();
    }
  }
  // Optional pinned host/workspace. "" = unset (server resolves at fire time).
  const [hostId, setHostId] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [scheduleUnsupported, setScheduleUnsupported] = useState(false);

  // Seed Name/Prompt on the closed→open transition ONLY. Keying off the
  // transition (not `open` being true) means we never clobber the user's edits
  // while the dialog stays open. Each fresh open is AUTHORITATIVE — the fields
  // are set to the initial values, or cleared to "" when none are supplied — so
  // a stale prefill can never leak into a subsequent manual open regardless of
  // how the prior instance was closed, and switching chips reseeds.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      if (editingTask) {
        const parsedSchedule = parseRRuleToScheduleModel(editingTask.rrule);
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
        setWorkspace(editingTask.workspace ?? "");
      } else {
        setName(initialName ?? "");
        setPrompt(initialPrompt ?? "");
        setPickedAgentId(null);
        setPickedModel("");
        setPickedEffort("");
        setPickedPermission("");
        setSchedule(DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(false);
        setHostId("");
        setWorkspace("");
      }
      setError(null);
    }
    wasOpen.current = open;
  }, [open, initialName, initialPrompt, editingTask]);

  const hostOptions = hosts ?? [];
  const preservePinnedHost = isEdit && editingTask?.hostId != null;
  // The resolved Host for the pinned id, or undefined when none is pinned.
  const selectedHost = hostId === "" ? undefined : hostOptions.find((h) => h.host_id === hostId);
  // Host whose `configured_harnesses` drives the picker's "needs setup" badges.
  // Host is optional on scheduled tasks; unset means resolve the connected host
  // at fire time, so we must not require the user to pin one before the
  // readiness affordance appears. Fall back to the first ONLINE host for badge
  // computation only — this does NOT change the form's `hostId` value (which
  // stays "" = resolve-at-fire); it just gives the picker a readiness map so
  // unconfigured agents show "needs setup" immediately, matching how the
  // interactive New Chat dialog auto-selects the first online host on mount.
  const badgeHost =
    selectedHost ?? hostOptions.find((h) => h.status === "online") ?? hostOptions[0];

  // A workspace is only valid with a host — mirror the server's pairing rule so
  // the user gets inline feedback instead of a 400.
  const workspaceWithoutHost = workspace.trim() !== "" && hostId === "";
  // Block submit on an invalid schedule (bad interval, empty multi-select) so
  // the form never posts an RRULE the server's validate_rrule would 400.
  const scheduleInvalid = scheduleUnsupported || validateSchedule(schedule) !== null;
  const mutationPending = createMutation.isPending || updateMutation.isPending;
  const canSubmit =
    name.trim() !== "" &&
    prompt.trim() !== "" &&
    (isEdit || effectiveAgentId !== null) &&
    !workspaceWithoutHost &&
    !scheduleInvalid &&
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
    setError(null);
    setScheduleUnsupported(false);
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetForm();
    onOpenChange(next);
  }

  async function handleSubmit() {
    setError(null);
    try {
      const input = {
        name: name.trim(),
        prompt: prompt.trim(),
        rrule: buildRRule(schedule),
        timezone: editingTask?.timezone ?? localTimezone(),
        ...(hostId !== "" ? { hostId } : {}),
        ...(hostId !== "" && workspace.trim() !== "" ? { workspace: workspace.trim() } : {}),
      };
      if (editingTask) {
        // Thread model/effort ONLY when the agent supports them. Each control's
        // "" (Default) maps to `null` so an update CLEARS a previously-set
        // override; a non-default pick sends the value. When the agent has no
        // model/effort surface we send neither key (left untouched server-side).
        const overrides = showModelEffort
          ? {
              modelOverride: pickedModel === "" ? null : pickedModel,
              reasoningEffort: pickedEffort === "" ? null : pickedEffort,
              permissionMode: pickedPermission === "" ? null : pickedPermission,
            }
          : {};
        await updateMutation.mutateAsync({
          id: editingTask.id,
          input: { ...input, ...overrides },
        });
      } else {
        if (effectiveAgentId === null) return;
        await createMutation.mutateAsync({
          ...input,
          agentId: effectiveAgentId,
          // Include an override only when the agent supports model/effort AND the
          // user picked a non-default value; an unselected control is omitted so
          // the create uses the agent's configured defaults.
          ...(showModelEffort && pickedModel !== "" ? { modelOverride: pickedModel } : {}),
          ...(showModelEffort && pickedEffort !== "" ? { reasoningEffort: pickedEffort } : {}),
          ...(showModelEffort && pickedPermission !== ""
            ? { permissionMode: pickedPermission }
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
              ? "Update this recurring agent session. It fires on a connected host."
              : "Runs an agent session on a recurring schedule. Fires on a connected host."}
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

          <div className="flex flex-col gap-1.5">
            {/* "Runs with" — the unified picker offers BOTH harnesses (Claude
                Code / Codex / Pi …) and agents (Polly / Debby), so "Agent" would
                be misleading. */}
            <Label>Runs with</Label>
            {isEdit ? (
              <div
                className="flex h-8 w-full items-center rounded-lg border border-input bg-transparent px-2.5 text-ui text-foreground dark:bg-input/30"
                data-testid="task-agent-readonly"
              >
                {agentLabel}
              </div>
            ) : (
              <div data-testid="task-agent-picker">
                <AgentHarnessPicker
                  agentEntries={agentEntries}
                  harnessEntries={harnessEntries}
                  effectiveAgentId={effectiveAgentId}
                  agentLabel={agentLabel}
                  hasAgents={agentList.length > 0}
                  // Drives the per-row "needs setup" badges from
                  // host.configured_harnesses. Uses the pinned host if any, else
                  // falls back to the first online host so the badges show in the
                  // fresh/default state (host is optional here — see `badgeHost`).
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
                  // Bound the dropdown height so it scrolls in the modal instead
                  // of running off the bottom of the screen (the trigger sits near
                  // the top of a tall dialog, unlike the composer footer). Width
                  // matches the interactive picker so the "needs setup" pills +
                  // agent descriptions fit without cramping (the shared default is
                  // only min-w-64; pin a comfortable fixed width like interactive).
                  contentClassName="max-h-80 w-80"
                  // Full-width trigger → left-align the menu's edge to it.
                  contentAlign="start"
                  // Match the sibling <Select> fields (Frequency / host): full
                  // width, bordered, h-8, normal foreground text — not the compact
                  // muted ghost styling the composer footer uses.
                  triggerClassName="h-8 w-full justify-between rounded-lg border border-input bg-transparent px-2.5 text-foreground hover:bg-transparent hover:text-foreground dark:bg-input/30"
                  triggerLabelClassName="max-w-none text-ui"
                />
              </div>
            )}
            {!showModelEffort && (
              <p className="text-sm text-muted-foreground">
                Uses this agent&apos;s default model, effort, and permission settings
              </p>
            )}
          </div>

          {/* Model + reasoning effort + permission mode — only for native
              coding agents that carry the model/effort surface (Claude Code).
              Unselected controls fall back to the agent's configured defaults. */}
          {showModelEffort && (
            <div data-testid="task-model-effort-field">
              <ModelEffortFields
                model={pickedModel}
                effort={pickedEffort}
                permissionMode={pickedPermission}
                hostId={hostId}
                onModelChange={setPickedModel}
                onEffortChange={setPickedEffort}
                onPermissionModeChange={setPickedPermission}
                onSelectOpenChange={handleSelectOpenChange}
              />
              <p className="mt-1.5 text-sm text-muted-foreground">
                Leave on Default to use the agent&apos;s configured model, effort, and permission
                mode. Automations run unattended, so a prompting mode (Manual or Plan) will wait for
                approval that never comes.
              </p>
            </div>
          )}

          <ScheduleFields
            model={schedule}
            onChange={(next) => {
              setScheduleUnsupported(false);
              setSchedule(next);
            }}
            onSelectOpenChange={handleSelectOpenChange}
          />
          {scheduleUnsupported && (
            <p className="text-sm text-destructive" role="alert">
              This schedule can&apos;t be edited in this form yet.
            </p>
          )}

          {/* Timezone is inferred from the browser (localTimezone via Intl) and
              intentionally has no visible control. It is still sent in the create
              payload so the schedule evaluates in the user's local zone. */}

          {/* Optional host + workspace pin. Left unset, the server resolves the
              owner's connected host and its home directory at fire time. */}
          <div className="flex flex-col gap-1.5" data-testid="task-host-field">
            <Label htmlFor="task-host">Host (optional)</Label>
            <Select
              value={hostId === "" ? UNSET_HOST : hostId}
              componentId="tasks.scheduled.host"
              onValueChange={(v) => {
                if (preservePinnedHost && v === UNSET_HOST) return;
                const next = v === UNSET_HOST ? "" : v;
                setHostId(next);
                // Clearing the host invalidates any pinned workspace.
                if (next === "") setWorkspace("");
              }}
              onOpenChange={handleSelectOpenChange}
            >
              <SelectTrigger id="task-host" data-testid="task-host-trigger" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={UNSET_HOST} disabled={preservePinnedHost}>
                  Resolve at fire time
                </SelectItem>
                {hostOptions.map((host) => (
                  <SelectItem key={host.host_id} value={host.host_id}>
                    {host.name} {host.status === "offline" ? "(offline)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Leave unset to run on your connected host when the task fires.
            </p>
          </div>

          {hostId !== "" && (
            <div className="flex flex-col gap-1.5">
              <Label>Workspace (optional)</Label>
              <p className="text-sm text-muted-foreground">
                Defaults to the host&apos;s home directory. Pick a directory to pin it.
              </p>
              <div className="h-56 overflow-hidden rounded-md border border-border">
                <WorkspacePicker
                  hostId={hostId}
                  onNavigate={setWorkspace}
                  initialPath={workspace || undefined}
                />
              </div>
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
            {isEdit ? "Save changes" : "Create task"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Sentinel Select value for "no pinned host" — Radix Select disallows "". */
const UNSET_HOST = "__unset_host__";

// The nested-dropdown dismiss guard now lives in a dependency-free module so
// other dialogs (project settings) can reuse it without pulling this file's
// module graph. Re-exported here to keep existing imports and tests stable.
export { isBackdropOverlay, isInsidePopper, shouldGuardDialogDismiss };
