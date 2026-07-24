// The "Set up manually" create dialog for a scheduled task. Fully wires the
// form to POST /v1/scheduled-tasks via useCreateScheduledTask, invalidating the
// list and closing on success. Reuses the existing agent picker
// (useAvailableAgents), host picker (useHosts), and directory picker
// (WorkspacePicker) rather than reinventing them.
//
// Field contract mirrors omnigent/server/routes/scheduled_tasks.py:
//   REQUIRED: name, prompt, rrule (built from the ScheduleFields model),
//     agent_id. OPTIONAL: timezone (default UTC), model_override,
//     reasoning_effort, host_id + workspace (validated as a pair — a workspace
//     without a host is rejected client- and server-side).

import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2Icon, TriangleAlertIcon } from "lucide-react";
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
import { WorkspacePicker } from "@/shell/WorkspacePicker";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useCreateScheduledTask } from "@/hooks/useScheduledTasks";
import { isNativeCodingAgent } from "@/lib/nativeCodingAgents";
import { sortAgentsForDisplay } from "@/lib/agentGrouping";
import {
  buildRRule,
  DEFAULT_SCHEDULE_MODEL,
  validateSchedule,
  type ScheduleModel,
} from "@/lib/scheduleBuilder";
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import { localTimezone } from "@/lib/timezones";

// Agents hidden from the scheduled-task picker (mirrors NewChatDialog's set):
// superseded / SDK-only harnesses that shouldn't be user-pickable here.
const HIDDEN_PICKER_AGENTS = new Set(["nessie", "kimi", "kimi-code"]);

export function CreateScheduledTaskDialog({
  open,
  onOpenChange,
  initialName,
  initialPrompt,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Seed values applied to Name/Prompt when the dialog opens (e.g. from a
   *  "Suggestions" suggestion chip). Omitted → the fields start empty. */
  initialName?: string;
  initialPrompt?: string;
}) {
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts } = useHosts({ enabled: open });
  const createMutation = useCreateScheduledTask();

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [schedule, setSchedule] = useState<ScheduleModel>(DEFAULT_SCHEDULE_MODEL);

  // ── Agent / harness picker (shared with NewChatDialog) ─────────────────────
  // The picker's "Harnesses" (Claude Code / Codex / Pi …) and "Agents"
  // (Polly / Debby / custom) rows are all real AvailableAgents with ids, so a
  // single `pickedAgentId` covers both cases — for a bare-harness pick it's the
  // `*-native-ui` agent's id, exactly what the interactive dialog sends.
  //
  // Scheduled tasks currently create sessions from the selected agent. Model,
  // effort, and permission controls are not offered here; upstream moved those into a separate
  // gear-icon HarnessConfigModal (NewChatDialog); reusing it is disproportionate
  // for a scheduled task (26 props, bound to smart-routing / cost-control /
  // dynamic model loading). A scheduled task only requires `agent_id`; model_override
  // / reasoning_effort are optional and simply omitted, so the fire path uses the
  // agent's configured defaults. Model/effort can be a follow-up if wanted.
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);

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
  const agentLabel = selectedAgent ? selectedAgent.display_name : "Select agent";

  function handleSelectAgent(agent: AvailableAgent) {
    setPickedAgentId(agent.id);
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

  // Seed Name/Prompt on the closed→open transition ONLY. Keying off the
  // transition (not `open` being true) means we never clobber the user's edits
  // while the dialog stays open. Each fresh open is AUTHORITATIVE — the fields
  // are set to the initial values, or cleared to "" when none are supplied — so
  // a stale prefill can never leak into a subsequent manual open regardless of
  // how the prior instance was closed, and switching chips reseeds.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setName(initialName ?? "");
      setPrompt(initialPrompt ?? "");
    }
    wasOpen.current = open;
  }, [open, initialName, initialPrompt]);

  const hostOptions = hosts ?? [];
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
  const scheduleInvalid = validateSchedule(schedule) !== null;
  const canSubmit =
    name.trim() !== "" &&
    prompt.trim() !== "" &&
    effectiveAgentId !== null &&
    !workspaceWithoutHost &&
    !scheduleInvalid &&
    !createMutation.isPending;

  function resetForm() {
    setName("");
    setPrompt("");
    setPickedAgentId(null);
    setSchedule(DEFAULT_SCHEDULE_MODEL);
    setHostId("");
    setWorkspace("");
    setError(null);
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetForm();
    onOpenChange(next);
  }

  async function handleSubmit() {
    if (effectiveAgentId === null) return;
    setError(null);
    try {
      await createMutation.mutateAsync({
        name: name.trim(),
        prompt: prompt.trim(),
        rrule: buildRRule(schedule),
        // Both a bare-harness pick and an agent pick resolve to a real agent id
        // here (harness rows are the `*-native-ui` agents), matching what the
        // interactive dialog sends as `agent_id`.
        agentId: effectiveAgentId,
        // Model/effort overrides are omitted so the fire path uses the selected
        // agent's configured defaults.
        // Timezone is inferred from the browser (Intl) and not user-editable in
        // this dialog. Still sent so the server evaluates the RRULE in the
        // user's local zone rather than defaulting to UTC.
        timezone: localTimezone(),
        // Send the host/workspace pair only when a host was pinned. A pinned
        // host with no workspace is allowed (server defaults to the host home).
        ...(hostId !== "" ? { hostId } : {}),
        ...(hostId !== "" && workspace.trim() !== "" ? { workspace: workspace.trim() } : {}),
      });
      handleOpenChange(false);
    } catch (err) {
      setError(
        err instanceof ScheduledTaskApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Couldn't create the scheduled task.",
      );
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-h-[90vh] overflow-y-auto sm:max-w-lg"
        data-testid="create-scheduled-task-dialog"
        // Keep a nested Select's dismiss (pick an option, OR click empty modal
        // body / trigger while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss` — it covers both the popper-target path and the
        // Select-open + focus-outside path, while leaving real backdrop clicks
        // and Escape to close as normal.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader>
          <DialogTitle>New scheduled task</DialogTitle>
          <DialogDescription>
            Runs an agent session on a recurring schedule. Fires on a connected host.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-1">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-name">Name</Label>
            <Input
              id="task-name"
              value={name}
              placeholder="Nightly triage"
              data-testid="task-name-input"
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-prompt">Prompt</Label>
            <Textarea
              id="task-prompt"
              value={prompt}
              rows={4}
              placeholder="What should the agent do each time it runs?"
              data-testid="task-prompt-input"
              // No native resize grip — match the clean styling of the other fields.
              className="resize-none"
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            {/* "Runs with" — the unified picker offers BOTH harnesses (Claude
                Code / Codex / Pi …) and agents (Polly / Debby), so "Agent" would
                be misleading. */}
            <Label>Runs with</Label>
            {/* Shared unified picker (same component the interactive NewChatDialog
                uses): a "Harnesses" section (Claude Code / Codex / Pi …) + an
                "Agents" section (Polly / Debby / custom), with per-entry
                model/effort knobs. onOpenChange feeds the dialog's dismiss guard
                so opening the picker doesn't close the modal. Custom-agent
                creation, sandbox, and the mode knobs that scheduled-tasks can't
                persist are wired to no-ops / dropped (see handleSubmit). */}
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
                // TODO(OMNI-1193): "Create custom agent" is a no-op here. The
                // interactive flow (NewChatDialog) opens CreateAgentDialog, holds
                // the returned bundle as a PENDING agent, and only PERSISTS it at
                // session-create time via the multipart createBundledSession
                // (which also mints a session). A scheduled task needs a concrete
                // `agent_id` up front and has no session to ride, and there is no
                // standalone "persist agent bundle → agent_id" endpoint yet — so
                // wiring this correctly is blocked on backend support (a persist
                // route, or a bundle-aware POST /v1/scheduled-tasks). Left inert
                // rather than forking a flow that creates a phantom session.
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
                triggerLabelClassName="max-w-none text-sm"
              />
            </div>
            <p className="text-[11px] text-muted-foreground">
              Uses this agent&apos;s default model, effort, and permission settings
            </p>
          </div>

          <ScheduleFields
            model={schedule}
            onChange={setSchedule}
            onSelectOpenChange={handleSelectOpenChange}
          />

          {/* Timezone is inferred from the browser (localTimezone via Intl) and
              intentionally has no visible control. It is still sent in the create
              payload so the schedule evaluates in the user's local zone. */}

          {/* Optional host + workspace pin. Left unset, the server resolves the
              owner's connected host and its home directory at fire time. */}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-host">Host (optional)</Label>
            <Select
              value={hostId === "" ? UNSET_HOST : hostId}
              onValueChange={(v) => {
                const next = v === UNSET_HOST ? "" : v;
                setHostId(next);
                // Clearing the host invalidates any pinned workspace.
                if (next === "") setWorkspace("");
              }}
              onOpenChange={handleSelectOpenChange}
            >
              <SelectTrigger id="task-host" data-testid="task-host-trigger">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={UNSET_HOST}>Resolve at fire time</SelectItem>
                {hostOptions.map((host) => (
                  <SelectItem key={host.host_id} value={host.host_id}>
                    {host.name} {host.status === "offline" ? "(offline)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted-foreground">
              Leave unset to run on your connected host when the task fires.
            </p>
          </div>

          {hostId !== "" && (
            <div className="flex flex-col gap-1.5">
              <Label>Workspace (optional)</Label>
              <p className="text-[11px] text-muted-foreground">
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
                <p className="truncate font-mono text-[11px] text-muted-foreground">{workspace}</p>
              )}
            </div>
          )}

          {workspaceWithoutHost && (
            <p
              className="flex items-center gap-1.5 text-xs text-destructive"
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
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!canSubmit}
            data-testid="create-scheduled-task-submit"
          >
            {createMutation.isPending && <Loader2Icon className="mr-1 size-4 animate-spin" />}
            Create task
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Sentinel Select value for "no pinned host" — Radix Select disallows "". */
const UNSET_HOST = "__unset_host__";

/**
 * True when an event target lives inside a Radix popper / Select portal (which
 * renders outside the DialogContent subtree). Used to distinguish a click that
 * merely closes a nested Select from a genuine outside-click on the backdrop, so
 * the former doesn't dismiss the whole Dialog.
 *
 * Exported for unit testing (the full portal outside-click is hard to reproduce
 * faithfully in jsdom — see the dialog test).
 */
export function isInsidePopper(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest(
      [
        "[data-radix-popper-content-wrapper]",
        '[data-slot="dropdown-menu-content"]',
        '[data-slot="popover-content"]',
        '[data-slot="select-content"]',
        '[role="listbox"]',
      ].join(", "),
    ) !== null
  );
}

/** True when the event target is the Dialog's backdrop overlay itself. A real
 *  backdrop click must always dismiss, so the guard lets it through. */
export function isBackdropOverlay(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('[data-slot="dialog-overlay"]') !== null;
}

/**
 * Pure decision for whether to SWALLOW the Dialog's outside-dismiss. Returns
 * true → preventDefault (dialog stays open); false → let it dismiss.
 *
 * A genuine backdrop-overlay click ALWAYS dismisses (returns false), even during
 * the grace window — this is the fix for backdrop-click-to-close being swallowed.
 * Otherwise we swallow only the narrow nested-dropdown cases: a dropdown
 * currently open, the trailing focus-outside within `graceMs` of a dropdown
 * closing, or a click that landed inside portalled dropdown content. Exported
 * pure so it's unit-testable without Radix's portal machinery.
 */
export function shouldGuardDialogDismiss(
  target: EventTarget | null,
  opts: { selectOpen: boolean; msSinceSelectClose: number; graceMs?: number },
): boolean {
  if (isBackdropOverlay(target)) return false;
  const graceMs = opts.graceMs ?? 150;
  return opts.selectOpen || opts.msSinceSelectClose < graceMs || isInsidePopper(target);
}
