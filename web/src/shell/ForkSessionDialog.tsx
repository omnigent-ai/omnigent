import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  MonitorCloudIcon,
  GitBranchIcon,
  InfoIcon,
  MonitorIcon,
  TriangleAlertIcon,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { forkSession, launchRunner } from "@/lib/sessionsApi";
import { useAvailableAgents, prefetchAvailableAgentDetails } from "@/hooks/useAvailableAgents";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { partitionAgentsByKind } from "@/lib/agentGrouping";
import { useSessionAgent } from "@/hooks/useAgents";
import { useSession } from "@/hooks/useSession";
import type { Session } from "@/lib/types";
import { useHosts, useHostModelOptions, type Host } from "@/hooks/useHosts";
import {
  nativeAgentHasCapability,
  nativeCodingAgentForAvailableAgent,
  nativeCodingAgentForSession,
} from "@/lib/nativeCodingAgents";
import {
  CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  CLAUDE_NATIVE_PERMISSION_MODES,
  claudePermissionModeFromSession,
} from "@/lib/claudePermissionMode";
import {
  AGY_NATIVE_DEFAULT_SKIP_MODE,
  AGY_NATIVE_SKIP_MODES,
  AGY_NATIVE_SKIP_VALUE,
  CODEX_NATIVE_APPROVAL_MODES,
  CODEX_NATIVE_BYPASS_APPROVAL_OPTION,
  CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
  CURSOR_NATIVE_DEFAULT_EXEC_MODE,
  CURSOR_NATIVE_EXEC_MODES,
  type NativeHarnessMode,
} from "@/lib/nativeHarnessModes";
import {
  CLAUDE_NATIVE_EFFORTS,
  DescribedSelect,
  EFFORT_SELECT_NONE,
  MODEL_SELECT_DEFAULT,
  RoutingModelSelect,
  defaultModelLabel,
  nativeModelLabel,
} from "@/components/HarnessConfigControls";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { agentRootName, forkTargetCarriesHistory, harnessFamily } from "@/lib/forkHarness";
import { checkHostDirectory, hostDirectoryMissing } from "@/hooks/useHostFilesystem";
import { getCliServerUrl } from "@/lib/host";
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import { WorkspacePathField } from "./WorkspacePathField";
import {
  ConnectHostInstructions,
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "./NewChatDialog";

// Select sentinel for "keep the source's agent" (Radix Select needs a
// non-empty value). When chosen, the fork omits agent_id and the server
// clones the source's agent.
const SAME_AS_SOURCE = "__same__";

/**
 * Compact host label for the Select item — mirrors NewChatDialog's
 * HostOption (which is private to that module).
 */
function HostLabel({ host }: { host: Host }) {
  const isOnline = host.status === "online";
  return (
    <span className="flex items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 text-muted-foreground" />
      )}
      <span className="font-mono text-sm">{host.name}</span>
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${
          isOnline ? "text-green-600" : "text-muted-foreground"
        }`}
      >
        <span
          className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
        />
        {host.status}
      </span>
    </span>
  );
}

/**
 * Split a server-created worktree path into the repo it came from and
 * the worktree's directory name, or null when the path doesn't look
 * like one. The host creates worktrees as
 * ``<parent>/<repo>-worktrees/<branch-dir>`` siblings of the repo
 * (``branch-dir`` is the sanitized branch name), so
 * ``/Users/a/proj-worktrees/fix`` → repo ``/Users/a/proj``,
 * branchDir ``fix``.
 */
function splitWorktreePath(workspace: string): { repo: string; branchDir: string } | null {
  const slash = workspace.lastIndexOf("/");
  if (slash <= 0 || slash === workspace.length - 1) return null;
  const parent = workspace.slice(0, slash);
  if (!parent.endsWith("-worktrees")) return null;
  const repo = parent.slice(0, -"-worktrees".length);
  if (!repo.includes("/") || repo.endsWith("/")) return null;
  return { repo, branchDir: workspace.slice(slash + 1) };
}

/**
 * Prefill for the fork's title input. Mirrors the server's
 * `"Fork of <title>"` derivation when the source has a title; when it
 * doesn't, returns "" so submitting omits the title and the server
 * derives it (rather than inventing a client-side placeholder).
 */
function defaultForkTitle(sourceTitle: string | null | undefined): string {
  const trimmed = sourceTitle?.trim();
  return trimmed ? `Fork of ${trimmed}` : "";
}

/**
 * A run-config field row that matches the dialog's "Agent" field: a stacked
 * `text-sm` muted label above a full-width control. Deliberately NOT the
 * `ConfigRow` used inside the "Configure …" modal (bigger `text-ui` label +
 * side-by-side sub-description) — those rows would read visually inconsistent
 * next to the Agent select in this same form.
 */
function ForkConfigRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}

/**
 * Ready-to-send run-config overrides from the fork dialog's pickers. An
 * undefined field is omitted from the fork request; the section reports the
 * whole value on every change so the form can read it back at submit.
 */
export interface ForkRunConfigValue {
  modelOverride?: string;
  reasoningEffort?: string;
  terminalLaunchArgs?: string[];
  /**
   * DANGEROUS codex full-bypass opt-in. Only emitted (as `true`) when the
   * user explicitly picks "Bypass approvals & sandbox" for a codex target;
   * the fork request carries it as `codex_bypass_sandbox` and the server
   * stamps the bypass label. Absent otherwise — the source's own bypass is
   * always dropped, so a fork is never silently armed.
   */
  codexBypassSandbox?: boolean;
}

/**
 * Model / effort / permission-mode pickers for a fork, mirroring the
 * new-session dialog's harness-config rows. Only rendered for a NATIVE target
 * harness (Claude Code / Codex / Pi / Cursor / Antigravity) — SDK and non-coding
 * targets have no per-session run knobs the fork can express.
 *
 * Seeding mirrors the two backend carry rules so the displayed value always
 * matches what the fork will do: model/effort seed from the source when the
 * target is in the same PROVIDER FAMILY (`sameFamilyAsSource`), and the
 * permission/approval/mode pickers seed from the source only on a same-AGENT
 * fork (`sameAgentAsSource`); otherwise each seeds the target's default. See
 * the parent for how those two flags are derived from the backend's
 * `copy_model_settings` / `copy_terminal_launch_args` gates.
 *
 * Emission is opt-in per control: a field is sent ONLY when the user actually
 * changed that control (see `touched`). An untouched control omits its field,
 * so the fork request carries nothing for it and the server's own
 * inherit / reset-by-family path decides — which is exactly the seeded meaning.
 * This is what keeps the section honest against the async model catalog: a
 * submit before the catalog resolves can't emit a spurious `model_override`.
 *
 * @param targetHarness - Effective target harness key (e.g. "claude-native"),
 *   or null for a non-native target (the section renders nothing).
 * @param targetAgent - The `{ name, harness }` the fork will bind, for
 *   capability detection.
 * @param sourceSession - Source session, read to seed the pickers.
 * @param sameFamilyAsSource - Whether the fork keeps the source's harness
 *   family; seeds model + effort (which the backend carries within a family).
 * @param sameAgentAsSource - Whether the fork keeps the source's exact agent;
 *   seeds the launch-arg pickers (permission / approval / mode), which the
 *   backend carries only on a same-agent fork.
 * @param selectedHostId - Host whose model catalog feeds the model picker; null
 *   leaves it on Default until a host is chosen.
 * @param onChange - Reports the ready-to-send value on every change.
 */
function ForkRunConfig({
  targetHarness,
  targetAgent,
  sourceSession,
  sameFamilyAsSource,
  sameAgentAsSource,
  selectedHostId,
  onChange,
}: {
  targetHarness: string | null;
  targetAgent: Pick<AvailableAgent, "name" | "harness">;
  sourceSession: Session | null;
  sameFamilyAsSource: boolean;
  sameAgentAsSource: boolean;
  selectedHostId: string | null;
  onChange: (value: ForkRunConfigValue) => void;
}) {
  const hasPermission = nativeAgentHasCapability(targetAgent, "permissionMode");
  const hasApproval = nativeAgentHasCapability(targetAgent, "approvalMode");
  const hasCursor = nativeAgentHasCapability(targetAgent, "cursorMode");
  const hasAgySkip = nativeAgentHasCapability(targetAgent, "skipPermissions");
  const hasModelPicker = nativeAgentHasCapability(targetAgent, "modelPicker");
  // Codex resolves its own catalog, so it gets a model row even though it lacks
  // the modelPicker capability (mirrors the new-session dialog).
  const isCodex = targetHarness === "codex-native";
  const showModel = hasModelPicker || isCodex;

  // Live model catalog for the target harness on the picked host. Only the
  // native harnesses that expose a picker resolve a catalog; others pass a
  // harmless unused harness key with the query disabled.
  const catalogHarness = showModel && targetHarness ? targetHarness : "claude-native";
  const { data: hostModelOptions, isLoading: modelsLoading } = useHostModelOptions(
    selectedHostId,
    catalogHarness,
    showModel && selectedHostId !== null,
  );
  const modelOptions = useMemo(
    () =>
      (hostModelOptions ?? []).map((option) => ({
        id: option.id,
        displayName: option.displayName ?? option.id,
        isDefault: option.isDefault,
      })),
    [hostModelOptions],
  );
  const modelSelectOptions = useMemo(
    () => modelOptions.map((m) => ({ id: m.id, label: nativeModelLabel(m) })),
    [modelOptions],
  );

  // Seed each picker: same-harness → the source's current value; switched →
  // the target harness's default. Recomputed when the target or seeding basis
  // changes so switching agents re-seeds correctly.
  const seededModel = useMemo(() => {
    // Seed the source's model only once the catalog confirms it: a model id
    // absent from the loaded options has no Select item to land on and would
    // blank the trigger. Until the catalog resolves, Default holds.
    const picked = sameFamilyAsSource ? sourceSession?.modelOverride : null;
    return picked && modelOptions.some((m) => m.id === picked) ? picked : MODEL_SELECT_DEFAULT;
  }, [sameFamilyAsSource, sourceSession, modelOptions]);
  const seededEffort = useMemo(() => {
    // Seed only an effort the picker can display; a source value outside the
    // offered vocabulary (e.g. "minimal") falls back to Default rather than
    // leaving the Select on an empty, unselectable value.
    const effort = sameFamilyAsSource ? sourceSession?.reasoningEffort : null;
    return effort && CLAUDE_NATIVE_EFFORTS.some((e) => e.value === effort)
      ? effort
      : EFFORT_SELECT_NONE;
  }, [sameFamilyAsSource, sourceSession]);
  const seededPermission = useMemo(() => {
    // Permission mode rides terminal_launch_args, which the backend copies
    // ONLY on a same-AGENT fork (copy_terminal_launch_args = not switching).
    // Seed on the same rule (not harness equality) so the displayed mode can't
    // diverge from what a same-harness/different-agent fork actually launches.
    if (sameAgentAsSource) {
      return (
        claudePermissionModeFromSession(sourceSession) ?? CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
      );
    }
    return CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE;
  }, [sameAgentAsSource, sourceSession]);
  const seededModeValue = useMemo(() => {
    // Same as permission: launch args carry over only on a same-agent fork, so
    // seed the mode from the source only then; otherwise the harness's default.
    const source = sameAgentAsSource ? (sourceSession?.terminalLaunchArgs ?? []) : [];
    const table: NativeHarnessMode[] = hasApproval
      ? CODEX_NATIVE_APPROVAL_MODES
      : hasCursor
        ? CURSOR_NATIVE_EXEC_MODES
        : hasAgySkip
          ? AGY_NATIVE_SKIP_MODES
          : [];
    const dflt = hasApproval
      ? CODEX_NATIVE_DEFAULT_APPROVAL_MODE
      : hasCursor
        ? CURSOR_NATIVE_DEFAULT_EXEC_MODE
        : AGY_NATIVE_DEFAULT_SKIP_MODE;
    // Codex bypass rides a LABEL, not launch args, so match it first: a
    // same-agent fork of a bypass-armed codex source seeds the bypass option.
    // (The source label is still dropped server-side; re-selecting here is the
    // explicit opt-in that re-arms it — never automatic.)
    if (
      isCodex &&
      sameAgentAsSource &&
      sourceSession?.labels?.["omnigent.codex_native.bypass_sandbox"] === "1"
    ) {
      return CODEX_NATIVE_BYPASS_APPROVAL_VALUE;
    }
    // Match the source's launch args against the mode table (longest args first
    // so "--mode plan" isn't shadowed by an empty-args default).
    const match = [...table]
      .sort((a, b) => b.args.length - a.args.length)
      .find((m) => m.args.length > 0 && m.args.every((arg) => source.includes(arg)));
    return match?.value ?? dflt;
  }, [isCodex, sameAgentAsSource, sourceSession, hasApproval, hasCursor, hasAgySkip]);

  const [model, setModel] = useState(seededModel);
  const [effort, setEffort] = useState(seededEffort);
  const [permission, setPermission] = useState(seededPermission);
  const [mode, setMode] = useState(seededModeValue);

  // Which controls the user has actually changed. An UNTOUCHED control omits
  // its field from the emitted config, so the fork request never carries it
  // and the server's inherit / reset-by-family path decides — which is exactly
  // the seeded meaning (same-harness → inherit the source; switch → the
  // target's default). This is what makes the section safe against the async
  // model catalog: a submit before `useHostModelOptions` resolves (or a source
  // model absent from the host's catalog) leaves the Model row on its "Default"
  // placeholder, but because it's untouched we send NOTHING rather than
  // `model_override: "default"` — so a fast clone can't silently reset the
  // source's model. Only a deliberate pick emits an explicit value (including
  // an explicit "Default", which then means clear-to-agent-default).
  const [touched, setTouched] = useState({
    model: false,
    effort: false,
    permission: false,
    mode: false,
  });
  const changeModel = (v: string) => {
    setTouched((t) => ({ ...t, model: true }));
    setModel(v);
  };
  const changeEffort = (v: string) => {
    setTouched((t) => ({ ...t, effort: true }));
    setEffort(v);
  };
  const changePermission = (v: string) => {
    setTouched((t) => ({ ...t, permission: true }));
    setPermission(v);
  };
  const changeMode = (v: string) => {
    setTouched((t) => ({ ...t, mode: true }));
    setMode(v);
  };

  // Re-seed whenever the seeding basis changes (agent switch, source load), but
  // never clobber a control the user already touched — a catalog refetch that
  // recomputes `seededModel` must not overwrite a manual pick.
  useEffect(() => {
    if (!touched.model) setModel(seededModel);
  }, [seededModel, touched.model]);
  useEffect(() => {
    if (!touched.effort) setEffort(seededEffort);
  }, [seededEffort, touched.effort]);
  useEffect(() => {
    if (!touched.permission) setPermission(seededPermission);
  }, [seededPermission, touched.permission]);
  useEffect(() => {
    if (!touched.mode) setMode(seededModeValue);
  }, [seededModeValue, touched.mode]);

  // Report the ready-to-send value on every change. Each field is included
  // ONLY when its control was touched (see `touched` above); an untouched
  // section therefore emits `{}` and the server inherits/resets as it would
  // for a fork that sent no run-config at all.
  useEffect(() => {
    const value: ForkRunConfigValue = {};
    if (showModel && touched.model) {
      value.modelOverride = model === MODEL_SELECT_DEFAULT ? "default" : model;
    }
    if (hasPermission) {
      if (touched.effort) {
        value.reasoningEffort = effort === EFFORT_SELECT_NONE ? "default" : effort;
      }
      if (touched.permission) {
        value.terminalLaunchArgs =
          permission === CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
            ? []
            : ["--permission-mode", permission];
      }
    } else if (hasApproval && touched.mode) {
      if (isCodex && mode === CODEX_NATIVE_BYPASS_APPROVAL_VALUE) {
        // Bypass is a LABEL, not launch args: clear any preset flags and set
        // the dedicated opt-in the server turns into the bypass label.
        value.terminalLaunchArgs = [];
        value.codexBypassSandbox = true;
      } else {
        value.terminalLaunchArgs =
          CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === mode)?.args ?? [];
      }
    } else if (hasCursor && touched.mode) {
      value.terminalLaunchArgs = CURSOR_NATIVE_EXEC_MODES.find((m) => m.value === mode)?.args ?? [];
    } else if (hasAgySkip && touched.mode) {
      value.terminalLaunchArgs = AGY_NATIVE_SKIP_MODES.find((m) => m.value === mode)?.args ?? [];
    }
    onChange(value);
  }, [
    showModel,
    hasPermission,
    hasApproval,
    hasCursor,
    hasAgySkip,
    isCodex,
    model,
    effort,
    permission,
    mode,
    touched,
    onChange,
  ]);

  if (
    targetHarness === null ||
    (!showModel && !hasPermission && !hasApproval && !hasCursor && !hasAgySkip)
  ) {
    return null;
  }

  return (
    <div className="flex flex-col gap-4" data-testid="fork-session-run-config">
      {showModel && (
        <ForkConfigRow label="Model">
          <RoutingModelSelect
            value={model}
            onValueChange={changeModel}
            offerSmartRouting={false}
            testId="fork-session-config-model"
            models={modelSelectOptions}
            defaultLabel={defaultModelLabel(modelOptions)}
            componentId="fork_session.config.model"
          >
            {modelsLoading && (
              <div className="px-2.5 py-1 text-sm text-muted-foreground">Loading models…</div>
            )}
            {!modelsLoading && modelOptions.length === 0 && (
              <div className="px-2.5 py-1 text-sm text-muted-foreground">
                {selectedHostId === null ? "Select a host to list models" : "Models unavailable"}
              </div>
            )}
          </RoutingModelSelect>
        </ForkConfigRow>
      )}

      {hasPermission && (
        <>
          <ForkConfigRow label="Effort">
            <Select
              value={effort}
              onValueChange={changeEffort}
              componentId="fork_session.config.effort"
              valueHasNoPii
            >
              <SelectTrigger
                className="w-full cursor-pointer"
                data-testid="fork-session-config-effort"
                aria-label="Reasoning effort"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
                {CLAUDE_NATIVE_EFFORTS.map((e) => (
                  <SelectItem key={e.value} value={e.value}>
                    {e.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </ForkConfigRow>

          <ForkConfigRow label="Permissions">
            <DescribedSelect
              value={permission}
              onValueChange={changePermission}
              options={CLAUDE_NATIVE_PERMISSION_MODES}
              testId="fork-session-config-permission"
              ariaLabel="Permissions"
              componentId="fork_session.config.permission"
            />
          </ForkConfigRow>
        </>
      )}

      {hasApproval && (
        <>
          <ForkConfigRow label="Approval">
            <DescribedSelect
              value={mode}
              onValueChange={changeMode}
              // Codex offers the DANGEROUS full-bypass as a 4th option, exactly
              // as the new-session dialog does; other approval harnesses list
              // only the three presets. Selecting it arms bypass on the fork
              // (a fresh, deliberate opt-in — the source's is always dropped).
              options={
                isCodex
                  ? [...CODEX_NATIVE_APPROVAL_MODES, CODEX_NATIVE_BYPASS_APPROVAL_OPTION]
                  : CODEX_NATIVE_APPROVAL_MODES
              }
              testId="fork-session-config-approval"
              ariaLabel="Approval"
              componentId="fork_session.config.approval"
            />
          </ForkConfigRow>
          {isCodex && mode === CODEX_NATIVE_BYPASS_APPROVAL_VALUE && (
            <div
              role="alert"
              data-testid="fork-session-codex-bypass-banner"
              className="flex items-start gap-1.5 rounded-md border border-destructive bg-destructive/10 px-2 py-1.5 text-xs font-medium leading-relaxed text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>
                Danger: this fork runs Codex with approvals and the command sandbox disabled. It can
                edit any file and run any command without asking.
              </span>
            </div>
          )}
        </>
      )}

      {hasCursor && (
        <ForkConfigRow label="Mode">
          <DescribedSelect
            value={mode}
            onValueChange={changeMode}
            options={CURSOR_NATIVE_EXEC_MODES}
            testId="fork-session-config-cursor-mode"
            ariaLabel="Mode"
            componentId="fork_session.config.cursor_mode"
          />
        </ForkConfigRow>
      )}

      {hasAgySkip && (
        <>
          <ForkConfigRow label="Permissions">
            <DescribedSelect
              value={mode}
              onValueChange={changeMode}
              options={AGY_NATIVE_SKIP_MODES}
              testId="fork-session-config-agy-skip"
              ariaLabel="Permissions"
              componentId="fork_session.config.permission"
            />
          </ForkConfigRow>
          {mode === AGY_NATIVE_SKIP_VALUE && (
            <div
              role="alert"
              data-testid="fork-session-agy-skip-banner"
              className="flex items-start gap-1.5 rounded-md border border-destructive bg-destructive/10 px-2 py-1.5 text-xs font-medium leading-relaxed text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>
                Danger: this session runs Antigravity with all tool permission prompts disabled. It
                can edit any file and run any command without asking.
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Clone/fork form — the single "fork + start" implementation, embedded
 * by {@link ForkSessionDialog} (the header-menu Clone dialog) and by the
 * ReconnectSessionDialog's Clone tab. Renders the scrollable field stack
 * plus its own footer (Cancel / Clone); the host dialog provides the
 * surrounding `DialogContent` and header.
 *
 * Forks the active (top-level) session via ``POST /v1/sessions/{id}/fork``
 * (the server deep-copies the transcript and clones the agent into a fresh
 * session owned by the caller; comments and permissions are NOT copied and
 * future messages don't mutate the source). For a *coding* source (one with
 * a working directory), the form also picks a host + directory + optional
 * git worktree and binds the fork to a runner via ``launchRunner``
 * (``POST /v1/hosts/{id}/runners``). For a non-coding source there is no
 * directory to pick, so it forks with just name + agent.
 *
 * Before creating anything, a coding fork pre-flights the picked directory
 * against the host (it must exist and be listable) — the launch below is
 * detached, so a bad path would otherwise produce a clone that silently
 * never starts. After that, the fork call is the only thing the form
 * awaits: on success it closes and
 * navigates into the clone IMMEDIATELY, and (for a coding source) fires the
 * runner launch in the background. Holding the dialog through the launch
 * blocks for as long as a worktree create takes (up to minutes) and hangs
 * forever on a dropped response, so the launch is detached. If it fails the
 * clone stays unbound and the user retries the bind via the session page's
 * directory picker (ChatPage's existing unbound-fork path). A fork-call
 * failure (nothing created) surfaces inline and the inputs stay editable for
 * a straight resubmit.
 *
 * Host/dir prefill from the *source*: its host is the default (when online)
 * and its workspace the default directory. When the source ran in a
 * server-created worktree, the prefill is instead the ORIGINAL repo as the
 * directory plus the source branch in the worktree field; submitted
 * untouched, the clone binds to the source's existing worktree directory
 * (renaming the branch creates a fresh worktree, automatically based off
 * the source branch). The Fork button greys out until a valid
 * online host + directory are chosen (no CLI fallback).
 *
 * All form state lives here, inside the dialog content, so closing the
 * dialog unmounts the form and resets it — no manual reset needed.
 *
 * @param sourceSessionId - Session being forked.
 * @param sourceTitle - Source title, used to prefill the fork's name.
 * @param sourceWorkspace - Source workspace; presence marks a coding source
 *   (shows the host/dir fields) and seeds the directory default.
 * @param sourceHostId - Source host; default host when it is online.
 * @param sourceGitBranch - Source git branch; drives the worktree prefill
 *   and the base ref for a renamed worktree branch.
 * @param upToResponseId - Truncation point for a "fork from here": the
 *   fork copies history only up to and including this response. `null` /
 *   omitted forks the full history.
 * @param onClose - Closes the host dialog (Cancel, and after a
 *   successful fork).
 */
export function ForkSessionForm({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  onClose,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Name is optional — left blank, the server derives "Fork of <source
  // title>" (shown as the input's placeholder). So the field starts empty.
  const [title, setTitle] = useState("");
  const [agentChoice, setAgentChoice] = useState<string>(SAME_AS_SOURCE);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Ready-to-send model/effort/permission overrides from the run-config
  // section. Empty until that section (rendered only for a native target)
  // reports its seeded value.
  const [runConfig, setRunConfig] = useState<ForkRunConfigValue>({});
  // Working directory + git worktree live behind "Advanced settings",
  // collapsed by default (they prefill sensibly from the source, so the
  // common "clone & start in the same place" path needs no input).
  const [showAdvanced, setShowAdvanced] = useState(false);
  // Auto-expands Advanced once when a directory conflict is detected, so the
  // warning + branch field aren't hidden. A ref (not state in the effect dep)
  // keeps it one-shot — the user can re-collapse it without it springing back.
  const autoExpandedRef = useRef(false);

  // A coding source ran in a working directory; only then does the fork
  // need a host + directory to start. A non-coding source forks with just
  // name + agent (no directory to pick).
  const isCodingSource = Boolean(sourceWorkspace);

  // Host/dir/worktree state — only meaningful for a coding source.
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  // Whether the "connect another host" CLI hint is expanded (only shown when
  // at least one host is online; otherwise the instructions render directly).
  const [showConnect, setShowConnect] = useState(false);

  // Built-in agents to switch to. The source session's bound agent gives
  // us its harness so we can offer only the targets that preserve
  // conversation history. (The form only mounts while its dialog is open,
  // so no extra enabled-gating on visibility is needed.)
  const { data: agents } = useAvailableAgents({ enabled: true });
  const { data: sourceAgent } = useSessionAgent(sourceSessionId);

  // Hosts for the picker — only for a coding source (a non-coding fork
  // never shows the host field).
  const { data: hosts } = useHosts({ enabled: isCodingSource });
  const allHosts = hosts ?? [];
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const offlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "offline"), [hosts]);
  const sourceHostOnline = onlineHosts.some((h) => h.host_id === sourceHostId);
  const serverUrl = getCliServerUrl();

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  // Whether the picked host is the SAME machine the source ran on. Only then
  // does "reuse the source's working directory" make sense — on a different
  // host that path is on someone else's machine. Drives the dir prefill, the
  // reuse-dir indicator, and whether Advanced starts collapsed.
  const onSourceHost = isCodingSource && selectedHostId !== null && selectedHostId === sourceHostId;
  const onDifferentHost =
    isCodingSource && selectedHostId !== null && selectedHostId !== sourceHostId;

  const sourceWorkspaceNorm = sourceWorkspace ? normalizeWorkspacePath(sourceWorkspace) : null;
  // Source ran in a server-created git worktree (its workspace IS the
  // worktree dir). Recover the repo the worktree was created from so the
  // form can present the pair as "original repo + worktree" rather than
  // the worktree path as the working directory. Recognized from the path
  // convention alone: a fork bound into an existing worktree carries no
  // gitBranch, so requiring one would miss fork-of-fork sources.
  const sourceWorktree =
    sourceWorkspaceNorm !== null ? splitWorktreePath(sourceWorkspaceNorm) : null;
  const sourceRepo = sourceWorktree?.repo ?? null;
  // Branch shown in the worktree field (and used as the new-branch base):
  // the session's recorded branch when present, else the worktree's
  // directory name — the sanitized branch it was created from.
  const sourceBranch = sourceGitBranch ?? sourceWorktree?.branchDir ?? null;

  // The source's bound agent, reduced to its ROOT name by peeling every
  // " (fork <id>)" / " (switch <id>)" clone suffix the fork/switch routes
  // append. A fork-of-a-fork or a switched session is named e.g.
  // "databricks_coding_agent (fork ag_a) (fork ag_b)" or
  // "claude-native-ui (switch ag_c)", neither of which matches a built-in by
  // name. agentRootName peels ALL layers — a single-layer / fork-only strip
  // (the previous regex here) would miss nested clones and every "(switch …)"
  // clone the in-place switch-agent flow creates — so the label resolves and
  // the dedup below still hides the source's own agent.
  const sourceAgentName = sourceAgent?.name ?? null;
  const sourceAgentBaseName = sourceAgentName ? agentRootName(sourceAgentName) : null;

  // Friendly label for the source's agent — the "same as source" option shows
  // this so the user sees the actual agent they're keeping. The source's YAML
  // name (e.g. "claude-native-ui") maps to a display name (e.g. "Claude Code")
  // via the built-in catalog; fall back to the raw name while it loads.
  const sourceAgentDisplay =
    (agents ?? []).find(
      (a) =>
        a.id === sourceAgent?.id || a.name === sourceAgentName || a.name === sourceAgentBaseName,
    )?.display_name ??
    sourceAgentBaseName ??
    sourceAgentName ??
    "the original agent";

  // Eagerly prefetch harness/description for session-discovered agents (those
  // with harness=null and a sessionId). Without this, forkTargetCarriesHistory
  // returns false for all of them and custom agents never appear in the picker.
  // prefetchAvailableAgentDetails is a no-op for agents whose harness is already
  // known, so re-running on every agents change is safe.
  useEffect(() => {
    for (const agent of agents ?? []) {
      void prefetchAvailableAgentDetails(agent, queryClient);
    }
  }, [agents, queryClient]);

  // Switch targets, excluding:
  //   1. the source's OWN agent — "Same as source" already represents
  //      keeping it, so listing it again is a confusing duplicate (e.g.
  //      a Claude Code session showing both "Same as source" and
  //      "Claude Code"). Matched by id and by name (including the
  //      fork-suffix-stripped name), since a UI session may bind the
  //      built-in directly, a same-named clone, or a "(fork …)" clone.
  //   2. targets that wouldn't preserve history: SDK targets replay the
  //      Omnigent transcript and native targets rebuild their on-disk
  //      transcript from the copied items (any source) — see
  //      forkTargetCarriesHistory. Unclassifiable harnesses
  //      (harness=null) are hidden.
  const switchableAgents = (agents ?? []).filter(
    (a) =>
      a.id !== sourceAgent?.id &&
      a.name !== sourceAgentName &&
      a.name !== sourceAgentBaseName &&
      forkTargetCarriesHistory(a.harness),
  );
  // Group the switch targets like the new-session picker: built-ins first,
  // then a divider, then custom agents — each sorted into display order.
  const { builtins: builtinSwitchable, customs: customSwitchable } = useMemo(
    () => partitionAgentsByKind(switchableAgents),
    [switchableAgents],
  );

  const switching = agentChoice !== SAME_AS_SOURCE;

  // Source session snapshot — seeds the run-config pickers on a same-harness
  // fork (its current model / effort / permission mode). Cheap: the chat page
  // already holds this in the shared ["session", id] cache.
  const { session: sourceSession } = useSession(sourceSessionId);

  // The agent the fork will bind: the switched-to target, else the source's
  // own agent. Its harness drives the run-config section (shown only for a
  // native target) and whether the pickers seed from the source.
  const targetAgent = useMemo<Pick<AvailableAgent, "name" | "harness"> | null>(() => {
    if (switching) return switchableAgents.find((a) => a.id === agentChoice) ?? null;
    if (sourceAgent) return { name: sourceAgent.name, harness: sourceAgent.harness ?? null };
    return null;
  }, [switching, switchableAgents, agentChoice, sourceAgent]);
  // Effective target harness key, resolved from the target agent (or the
  // source session's wrapper label when keeping the source's agent — a UI
  // session's bound agent may report a null harness).
  const targetHarness = useMemo(() => {
    const fromAgent = nativeCodingAgentForAvailableAgent(targetAgent)?.harness;
    if (fromAgent) return fromAgent;
    if (!switching) return nativeCodingAgentForSession(sourceSession)?.harness ?? null;
    return null;
  }, [targetAgent, switching, sourceSession]);
  // The two backend carry rules the pickers must mirror so the displayed value
  // matches what the fork actually does:
  //
  //  • Model / effort carry over within the same provider FAMILY (backend
  //    `copy_model_settings = !switching || _same_provider_family`). Keyed on
  //    provider family, NOT native-harness identity — otherwise a same-family
  //    switch (e.g. Claude-SDK → Claude Code, whose SDK source has no native
  //    wrapper) would seed "Default" while the backend silently inherits the
  //    source's model/effort. Use each side's EFFECTIVE harness
  //    (`sourceSession.harness`, non-null for SDK sources) → `harnessFamily`.
  //  • Launch args (permission / approval / mode) carry over only on a
  //    same-AGENT fork (`copy_terminal_launch_args = not switching_agent`).
  const sourceFamily = harnessFamily(sourceSession?.harness);
  const targetFamily = harnessFamily(targetHarness);
  const sameFamilyAsSource = !switching || (sourceFamily !== null && sourceFamily === targetFamily);
  const sameAgentAsSource = !switching;

  // Default the host = source host (when online) else the first online
  // host, once hosts have loaded. Only fills an empty slot so an explicit
  // pick is never overridden.
  useEffect(() => {
    if (!isCodingSource || selectedHostId !== null) return;
    if (sourceHostId && sourceHostOnline) {
      setSelectedHostId(sourceHostId);
    } else if (onlineHosts.length > 0) {
      setSelectedHostId(onlineHosts[0].host_id);
    }
  }, [isCodingSource, selectedHostId, sourceHostId, sourceHostOnline, onlineHosts]);

  // Prefill the directory with the source's workspace — but only when staying
  // on the source host. On a different host that path is a different machine,
  // so leave it blank for the user to pick. A worktree-backed source prefills
  // as its ORIGINAL repo + the source branch in the worktree field (the pair
  // its workspace was created from), not the raw worktree path.
  useEffect(() => {
    if (!onSourceHost || workspace !== "" || !sourceWorkspace) return;
    if (sourceRepo !== null && sourceBranch !== null) {
      setWorkspace(sourceRepo);
      setBranchName(sourceBranch);
    } else {
      setWorkspace(sourceWorkspace);
    }
  }, [onSourceHost, workspace, sourceWorkspace, sourceRepo, sourceBranch]);

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);
  // The prefilled repo + source-branch pair left untouched: that branch
  // already exists (with a live worktree), so instead of asking the server
  // to create it — which would fail — the clone binds straight to the
  // source's existing worktree directory, exactly like reusing a plain
  // source directory.
  const usingSourceWorktree =
    onSourceHost &&
    sourceRepo !== null &&
    sourceBranch !== null &&
    workspaceTrimmed === sourceRepo &&
    branchName.trim() === sourceBranch;
  // Directory the clone will actually start in — feeds the conflict check,
  // the reuse-dir tooltip, the pre-flight, and the launch itself.
  const effectiveWorkspace = usingSourceWorktree ? (sourceWorkspaceNorm ?? "") : workspaceTrimmed;
  // The picked host must still be ONLINE, not merely selected: hosts refetch
  // periodically, so a previously-picked host can go offline while selected.
  // Gating on online-ness keeps the button greyed (and avoids a launchRunner
  // that would just fail server-side) until a live host is chosen.
  const selectedHostOnline =
    selectedHostId !== null && onlineHosts.some((h) => h.host_id === selectedHostId);
  // A coding source can only start once a live host + valid directory are picked.
  const canSubmit = !isCodingSource || (selectedHostOnline && workspaceValid);

  // Conflict hint: other *connected* sessions already working in the picked
  // directory on this host (same wiring as NewChatDialog).
  const { data: directorySessions } = useDirectorySessions(
    isCodingSource && Boolean(selectedHostId),
  );
  const conflictCandidates = useMemo(
    () =>
      isCodingSource
        ? (directorySessions ?? []).filter(
            (s) => s.host_id === selectedHostId && s.workspace != null,
          )
        : [],
    [isCodingSource, directorySessions, selectedHostId],
  );
  const runnerHealth = useRunnerHealthRegistration(conflictCandidates);
  const conflictingSessions = useMemo(
    () =>
      sessionsSharingDirectory(
        conflictCandidates,
        selectedHostId,
        effectiveWorkspace,
        (id) => runnerHealth.get(id) === true,
      ),
    [conflictCandidates, selectedHostId, effectiveWorkspace, runnerHealth],
  );
  // A NEW branch means an isolated worktree, so no conflict; reusing the
  // source's existing worktree shares its directory like a blank branch does.
  const showConflictHint =
    (branchName.trim() === "" || usingSourceWorktree) && conflictingSessions.length > 0;

  // Reveal Advanced (once) only when running on a DIFFERENT host than the
  // source — a fresh directory must be picked there, so the field can't stay
  // hidden. On the source host it stays collapsed (the defaults need no
  // input); a directory conflict is surfaced inline at the top instead of
  // force-opening Advanced, since cloning a *running* session always trips it
  // (the original is still in that directory).
  useEffect(() => {
    if (onDifferentHost && !autoExpandedRef.current) {
      autoExpandedRef.current = true;
      setShowAdvanced(true);
    }
  }, [onDifferentHost]);

  // Mismatched-directory warning: the transcript's file references were
  // grounded in the source's directory ON the source's host. A different
  // directory — or a different host, where even an identical path is a
  // different machine — won't resolve them, so the agent must re-orient.
  const hostMismatch =
    sourceHostId != null && selectedHostId !== null && selectedHostId !== sourceHostId;
  const showMismatchWarning =
    isCodingSource &&
    ((hostMismatch && workspaceTrimmed !== "") ||
      (sourceWorkspaceNorm !== null &&
        workspaceTrimmed !== "" &&
        workspaceTrimmed !== sourceWorkspaceNorm &&
        // The source's original repo (which worktree sources prefill) is
        // the same lineage as its worktree — not a mismatch.
        (sourceRepo === null || workspaceTrimmed !== sourceRepo)));

  // Default state: a coding clone on the source host still pointed at the
  // source's directory. Drives the "reuses the original's working directory"
  // indicator, which explains the default without forcing Advanced open. On a
  // different host this is false (the mismatch warning takes over instead).
  const usingSourceDir = onSourceHost && workspaceTrimmed !== "" && !showMismatchWarning;

  function commitWorkspacePath(path: string): void {
    setWorkspace(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
  }

  async function handleFork(): Promise<void> {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      // Pre-flight the directory BEFORE creating anything: the runner
      // launch below is detached and its failure is swallowed, so a
      // nonexistent path would otherwise leave a clone that silently
      // never starts.
      let recreateSourceWorktree = false;
      if (isCodingSource && selectedHostId) {
        const problem = await checkHostDirectory(selectedHostId, effectiveWorkspace);
        if (problem !== null) {
          // Deleted source worktree + untouched name: recreate the worktree
          // at the same path/branch instead of erroring — the host's
          // create-worktree handles an already-existing branch (no -b).
          // Only this exact case falls back; every other problem (offline
          // host, unlistable path, network) still aborts the fork.
          if (
            usingSourceWorktree &&
            (await hostDirectoryMissing(selectedHostId, effectiveWorkspace))
          ) {
            // The recreate launches from the repo path, so pre-flight THAT
            // path too — a missing repo can't recreate anything, and the
            // detached launch below swallows its failure.
            const repoProblem = await checkHostDirectory(selectedHostId, workspaceTrimmed);
            if (repoProblem !== null) {
              setError(repoProblem);
              return;
            }
            recreateSourceWorktree = true;
          } else {
            setError(problem);
            return;
          }
        }
      }
      const trimmed = title.trim();
      // Empty title → omit so the server derives "Fork of <source title>".
      // The run-config section (native targets only) reports its ready-to-send
      // value; an empty object (non-native target) sends no run overrides.
      const fork = await forkSession(
        sourceSessionId,
        trimmed === "" ? undefined : trimmed,
        switching ? agentChoice : undefined,
        upToResponseId ?? undefined,
        runConfig,
      );
      // Coding fork: launch the runner in the BACKGROUND, then navigate
      // into the (already-created, unbound) clone immediately — awaiting the
      // launch would block the modal for a worktree create (up to minutes)
      // and hang on a dropped response. If the launch fails the clone stays
      // unbound; ChatPage's existing unbound-fork path lets the user retry
      // the bind via the directory picker. (A follow-up will surface the
      // failure proactively + show "Connecting…" for the whole launch.)
      if (isCodingSource && selectedHostId) {
        const trimmedBranch = branchName.trim();
        addRecent(workspaceTrimmed);
        // Reusing the source's worktree binds its directory directly (no
        // git options — the branch already exists, creating it would fail).
        // A NEW worktree is based on the source's branch so the clone
        // continues from the original's committed work — but only when the
        // picked directory is the source's own repo (on the source host);
        // elsewhere that ref can't be assumed to exist.
        const baseOnSource =
          onSourceHost &&
          (workspaceTrimmed === sourceRepo || workspaceTrimmed === sourceWorkspaceNorm);
        void launchRunner(
          selectedHostId,
          fork.id,
          // Recreating a deleted source worktree launches from the REPO
          // path (the server derives the worktree directory from the
          // branch), exactly like the renamed-branch path.
          recreateSourceWorktree ? workspaceTrimmed : effectiveWorkspace,
          trimmedBranch !== "" && (!usingSourceWorktree || recreateSourceWorktree)
            ? recreateSourceWorktree
              ? // The branch survives its deleted directory — recreate the
                // worktree by checking the existing branch back out (no base:
                // nothing new is forked).
                { branchName: trimmedBranch, existingBranch: true }
              : {
                  branchName: trimmedBranch,
                  baseBranch: baseOnSource && sourceBranch ? sourceBranch : undefined,
                }
            : undefined,
        ).catch((e) => {
          // Swallow: recovery is the unbound-fork picker on the session
          // page. Logged so a failed launch isn't entirely silent.
          console.warn(`Clone ${fork.id}: background runner launch failed`, e);
        });
      }
      // Fire-and-forget: the sidebar refresh must not gate navigation.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // The fork inherits the source's project, and each folder renders its
      // own ["project-sessions", <name>] list. The WS fallback can't converge
      // it either: it skips the active session, and the navigate below makes
      // the fork active.
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      onClose();
      navigate(`/c/${fork.id}`);
    } catch (e) {
      // forkSession failed — nothing created, so inputs stay editable for a resubmit.
      setError(e instanceof Error ? e.message : "Couldn't clone the session. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // Suggested name shown as the input placeholder; blank input → the server
  // applies this same "Fork of <source title>" default.
  const namePlaceholder = defaultForkTitle(sourceTitle) || "Name the cloned session";

  return (
    <>
      <div className="-mr-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-4 [scrollbar-width:thin] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
        {/* Host first: with no online host there is nothing to run the
              clone on, so the user learns up front whether they can proceed.
              Mirrors NewChatDialog: a picker when hosts are online (with a
              collapsible "connect another" CLI hint), or the connect
              instructions directly when none are. */}
        {isCodingSource && (
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted-foreground">Host</span>
            {hosts === undefined ? (
              <p className="text-sm text-muted-foreground" data-testid="fork-session-no-hosts">
                Loading hosts…
              </p>
            ) : onlineHosts.length === 0 ? (
              // Nothing usable (no hosts, or all offline) — show the connect
              // command directly so the user can unblock. The submit button
              // stays greyed until a host is online.
              <ConnectHostInstructions
                serverUrl={serverUrl}
                label={
                  allHosts.length === 0
                    ? "No hosts connected yet. Connect one from your terminal:"
                    : "No hosts online. Reconnect from your terminal to start the clone:"
                }
              />
            ) : (
              <>
                <Select
                  value={selectedHostId ?? ""}
                  componentId="fork_session.host"
                  onValueChange={(v) => {
                    setSelectedHostId(v);
                    // Workspace and the worktree branch are host-specific:
                    // the directory path and the prefilled source branch only
                    // make sense on the source machine. Clear both on a host
                    // change. (The source-host prefill effect re-seeds them
                    // if the user switches back.)
                    setWorkspace("");
                    setBranchName("");
                    setBrowsing(false);
                  }}
                >
                  <SelectTrigger className="w-full text-sm" data-testid="fork-session-host-select">
                    <SelectValue placeholder="Select a host" />
                  </SelectTrigger>
                  <SelectContent>
                    {onlineHosts.map((host) => (
                      <SelectItem
                        key={host.host_id}
                        value={host.host_id}
                        data-testid={`fork-session-host-option-${host.host_id}`}
                      >
                        <HostLabel host={host} />
                      </SelectItem>
                    ))}
                    {offlineHosts.map((host) => (
                      <SelectItem
                        key={host.host_id}
                        value={host.host_id}
                        disabled
                        data-testid={`fork-session-host-option-${host.host_id}`}
                      >
                        <HostLabel host={host} />
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <button
                  type="button"
                  onClick={() => setShowConnect((v) => !v)}
                  className="flex cursor-pointer items-center gap-1 self-start text-sm text-muted-foreground transition hover:text-foreground"
                  data-testid="fork-session-connect-host-toggle"
                >
                  {showConnect ? (
                    <ChevronUpIcon className="size-3.5" />
                  ) : (
                    <ChevronDownIcon className="size-3.5" />
                  )}
                  Connect another host from your terminal
                </button>
                {showConnect && <ConnectHostInstructions serverUrl={serverUrl} />}
              </>
            )}
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <label htmlFor="fork-session-agent" className="text-sm font-medium text-muted-foreground">
            Agent
          </label>
          <Select
            value={agentChoice}
            onValueChange={setAgentChoice}
            componentId="fork_session.agent"
          >
            <SelectTrigger
              id="fork-session-agent"
              data-testid="fork-session-agent-select"
              className="w-full text-sm"
            >
              {/* Custom value so the default reads "<agent> (same as original
                    session)" with the parenthetical greyed, mirroring the option. */}
              <SelectValue>
                {switching ? (
                  (switchableAgents.find((a) => a.id === agentChoice)?.display_name ??
                  sourceAgentDisplay)
                ) : (
                  <>
                    {sourceAgentDisplay}{" "}
                    <span className="text-muted-foreground">(same as original session)</span>
                  </>
                )}
              </SelectValue>
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              <SelectItem
                value={SAME_AS_SOURCE}
                data-testid="fork-session-agent-option-same"
                className="text-sm"
              >
                {sourceAgentDisplay}{" "}
                <span className="text-muted-foreground">(same as original session)</span>
              </SelectItem>
              {builtinSwitchable.map((agent) => (
                <SelectItem
                  key={agent.id}
                  value={agent.id}
                  data-testid={`fork-session-agent-option-${agent.id}`}
                  className="text-sm"
                >
                  {agent.display_name}
                </SelectItem>
              ))}
              {/* Divider between the built-in group and the custom group,
                  only when both are present (mirrors NewChatDialog). */}
              {builtinSwitchable.length > 0 && customSwitchable.length > 0 && <SelectSeparator />}
              {customSwitchable.map((agent) => (
                <SelectItem
                  key={agent.id}
                  value={agent.id}
                  data-testid={`fork-session-agent-option-${agent.id}`}
                  className="text-sm"
                >
                  {agent.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Run config (native targets only): model / effort / permission mode.
              Seeds from the source on a same-harness fork, else from the target
              harness's defaults. Renders nothing for a non-native target.
              key=agentChoice remounts on any agent switch so the pickers'
              touched-state and values reset and re-seed from scratch — otherwise
              a model picked for one harness would leak onto the next and defeat
              the backend's cross-family reset. */}
        {targetAgent !== null && (
          <ForkRunConfig
            key={agentChoice}
            targetHarness={targetHarness}
            targetAgent={targetAgent}
            sourceSession={sourceSession}
            sameFamilyAsSource={sameFamilyAsSource}
            sameAgentAsSource={sameAgentAsSource}
            selectedHostId={isCodingSource ? selectedHostId : (sourceHostId ?? null)}
            onChange={setRunConfig}
          />
        )}

        {/* Indicator: by default the clone reuses the source's working
              directory; changing it lives under Advanced settings. */}
        {usingSourceDir && (
          <p className="text-sm text-muted-foreground" data-testid="fork-session-reuse-dir-hint">
            By default the clone reuses the original session's{" "}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="cursor-pointer underline decoration-dotted underline-offset-2"
                  data-testid="fork-session-reuse-dir-path"
                >
                  working directory
                </button>
              </TooltipTrigger>
              <TooltipContent className="font-mono break-all">{effectiveWorkspace}</TooltipContent>
            </Tooltip>
            . Open Advanced settings to change it.
          </p>
        )}

        {/* Conflict warning at the top level (not inside Advanced) so it's
              visible without expanding — cloning a running session always
              shares its directory with the still-active original. */}
        {showConflictHint && (
          <p
            className="flex items-start gap-1.5 text-sm text-warning"
            data-testid="fork-session-conflict-hint"
          >
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {conflictingSessions.length === 1
                ? "1 other agent is"
                : `${conflictingSessions.length} other agents are`}{" "}
              working in this directory, so writes may conflict. Name a{" "}
              {usingSourceWorktree ? "different git branch" : "git branch"} under Advanced settings
              to work in an isolated copy.
            </span>
          </p>
        )}

        {/* Name and (for coding sources) working directory + git worktree
              live behind Advanced, collapsed by default — everything here
              prefills sensibly, so the common path needs no input. Mirrors
              NewChatDialog's advanced toggle. */}
        <div className="flex flex-col gap-4">
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="flex cursor-pointer items-center gap-1 self-start text-sm font-medium text-foreground transition hover:text-foreground"
            data-testid="fork-session-advanced-toggle"
            aria-expanded={showAdvanced}
            aria-controls="fork-session-advanced-content"
          >
            {showAdvanced ? (
              <ChevronUpIcon className="size-3.5" />
            ) : (
              <ChevronDownIcon className="size-3.5" />
            )}
            Advanced settings
          </button>

          {showAdvanced && (
            <div
              id="fork-session-advanced-content"
              className="flex flex-col gap-4"
              data-testid="fork-session-advanced-content"
            >
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="fork-session-title"
                  className="text-sm font-medium text-muted-foreground"
                >
                  Name (optional)
                </label>
                <input
                  id="fork-session-title"
                  data-testid="fork-session-title-input"
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !submitting && canSubmit) handleFork();
                  }}
                  placeholder={namePlaceholder}
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
                />
              </div>

              {isCodingSource && (
                <>
                  <div className="flex flex-col gap-2">
                    <span className="text-sm font-medium text-muted-foreground">
                      Working directory
                    </span>
                    {selectedHostId ? (
                      <>
                        <WorkspacePathField
                          hostId={selectedHostId}
                          value={workspace}
                          onChange={setWorkspace}
                          onBrowse={() => setBrowsing((v) => !v)}
                          onCommit={commitWorkspacePath}
                          recent={recent}
                          dropdownDisabled={browsing}
                        />
                        {browsing && (
                          <WorkspacePicker
                            key={browseNonce}
                            hostId={selectedHostId}
                            initialPath={
                              isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined
                            }
                            onSelect={(path) => {
                              setWorkspace(path);
                              setBrowsing(false);
                            }}
                            onClose={() => setBrowsing(false)}
                          />
                        )}
                        {showMismatchWarning && (
                          <p
                            className="flex items-start gap-1.5 text-sm text-warning"
                            data-testid="fork-session-mismatch-warning"
                          >
                            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                            <span>
                              This directory differs from the original session's. Earlier file
                              references in the transcript may not apply — the agent will need to
                              re-orient.
                            </span>
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        Select a host to choose a directory.
                      </p>
                    )}
                  </div>

                  <div className="flex flex-col gap-1">
                    <label
                      htmlFor="fork-session-branch"
                      className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground"
                    >
                      <GitBranchIcon className="size-3.5" />
                      Git worktree (optional)
                    </label>
                    <input
                      id="fork-session-branch"
                      type="text"
                      value={branchName}
                      onChange={(e) => setBranchName(e.target.value)}
                      placeholder="feature/my-branch"
                      data-testid="fork-session-branch-input"
                      className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
                    />
                    <p className="text-sm text-muted-foreground">
                      {usingSourceWorktree
                        ? "The clone starts in the original session's existing worktree for this " +
                          "branch. Name a different branch to work in an isolated copy."
                        : "Creates a git worktree for a new branch in an isolated directory — " +
                          "keeps the clone from fighting the original over the same files. Leave " +
                          "blank to start in the picked directory."}
                    </p>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {error !== null && (
        <p data-testid="fork-session-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button
          data-testid="fork-session-submit"
          onClick={handleFork}
          loading={submitting}
          disabled={!canSubmit}
        >
          {isCodingSource ? "Clone & start" : "Clone"}
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Clone/fork dialog for a session — the header menu's Clone surface.
 * A thin `Dialog` shell (title + info tooltip) around
 * {@link ForkSessionForm}, which holds all the fork logic and state.
 * The form lives inside `DialogContent`, so closing the dialog unmounts
 * and resets it.
 *
 * @param sourceSessionId - Session being forked.
 * @param sourceTitle - Source title, used to prefill the fork's name.
 * @param sourceWorkspace - Source workspace; presence marks a coding source
 *   (shows the host/dir fields) and seeds the directory default.
 * @param sourceHostId - Source host; default host when it is online.
 * @param sourceGitBranch - Source git branch; drives the worktree prefill
 *   and the base ref for a renamed worktree branch.
 * @param upToResponseId - Truncation point for a "fork from here" opened
 *   from a message's actions: the fork copies history only up to and
 *   including this response. `null` / omitted clones the full history.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 */
export function ForkSessionDialog({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  open,
  onOpenChange,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const truncated = upToResponseId != null;
  // Shown in the title's info tooltip (and a visually-hidden DialogDescription
  // for screen readers). Coding sources also start on a picked host/directory.
  const cloneDescription = `${
    truncated
      ? "Copies this session's history up to the selected response into a new session you own — messages after it aren't carried over"
      : "Copies this session's history into a new session you own"
  }${
    sourceWorkspace ? ", then starts it on the host and directory you pick" : ""
  }. Comments aren't copied, and changes in the clone won't affect the original.`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="fork-session-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-1.5">
            {truncated ? "Fork from this response" : "Clone session"}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label="What does cloning do?"
                  data-testid="fork-session-info"
                  // tabIndex=-1 keeps the dialog's open-autofocus (and tabbing)
                  // off this icon, so the tooltip only opens on hover — not the
                  // moment the modal appears. The same text lives in the
                  // sr-only DialogDescription below, so AT users still get it.
                  tabIndex={-1}
                  className="cursor-pointer text-muted-foreground transition-colors hover:text-foreground"
                >
                  <InfoIcon className="size-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{cloneDescription}</TooltipContent>
            </Tooltip>
          </DialogTitle>
          {/* Description moved into the title's info tooltip; kept here visually
              hidden so the dialog stays described for screen readers. */}
          <DialogDescription className="sr-only">{cloneDescription}</DialogDescription>
        </DialogHeader>
        <ForkSessionForm
          sourceSessionId={sourceSessionId}
          sourceTitle={sourceTitle}
          sourceWorkspace={sourceWorkspace}
          sourceHostId={sourceHostId}
          sourceGitBranch={sourceGitBranch}
          upToResponseId={upToResponseId}
          onClose={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}
