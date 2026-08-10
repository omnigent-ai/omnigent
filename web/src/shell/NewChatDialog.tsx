import {
  type DragEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  MonitorIcon,
  MonitorCloudIcon,
  CircleHelpIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  GitBranchIcon,
  ArrowUpIcon,
  Loader2Icon,
  FileTextIcon,
  FolderIcon,
  ImageIcon,
  PaperclipIcon,
  PlusIcon,
  SettingsIcon,
  ShuffleIcon,
  TriangleAlertIcon,
  XIcon,
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
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  CLAUDE_NATIVE_EFFORTS,
  ConfigRow,
  DescribedSelect,
  EFFORT_SELECT_NONE,
  EFFORT_UNAVAILABLE_PLACEHOLDER,
  MODEL_SELECT_DEFAULT,
  MODEL_SELECT_SMART,
  RoutingModelSelect,
} from "@/components/HarnessConfigControls";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { authenticatedFetch } from "@/lib/identity";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { attachmentKey } from "@/lib/attachments";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { HarnessSetupDialog } from "@/shell/HarnessSetupDialog";
import {
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  harnessWarningBadgeText,
  isCodexHarness,
  isNativeCursorHarness,
} from "@/lib/harnessSetup";

// Re-exported for tests that import the readiness helpers from this module.
export { harnessUnavailableReasonOnHost, harnessUnconfiguredOnHost, harnessWarningBadgeText };
import { sandboxOptionLabel } from "@/lib/capabilities";
import {
  isSlashCommandText,
  rankedSlashCommandNames,
  SlashCommandMenu,
} from "@/components/SlashCommandMenu";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { appendPromptHistoryEntry } from "@/hooks/usePromptHistory";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { CliCommandBlock } from "./CliCommandBlock";
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import {
  initialPrefillState,
  prefillDone,
  projectPrefillStep,
  type ProjectPrefillConfig,
  type ProjectPrefillState,
} from "./projectPrefill";
import { getCliServerUrl, getOmnigentHostConfig } from "@/lib/host";
import { readLastAgentId, writeLastAgentId } from "@/lib/agentPreferences";
import {
  readLastHostChoice,
  writeLastHostChoice,
  SANDBOX_HOST_CHOICE,
} from "@/lib/hostPreferences";
import { readLastHarness, writeLastHarness } from "@/lib/harnessPreferences";
import { readHideUnconfiguredHarnesses } from "@/lib/harnessVisibilityPreferences";
import { readDefaultBaseBranch } from "@/lib/baseBranchPreferences";
import { readHarnessOptions, writeHarnessOption } from "@/lib/modePreferences";
import {
  AUTO_HARNESS_DESCRIPTION,
  AUTO_HARNESS_ID,
  AUTO_NATIVE_HARNESS_ID,
  isAutoHarness,
  SMART_ROUTING_LABEL,
  useBrainHarnessLabels,
} from "@/lib/agentLabels";
import {
  SMART_ROUTING_ARMS,
  hostBacksHarnessWithGateway,
  smartRoutingDroppedMessage,
  smartRoutingSourceFor,
  smartRoutingUnavailableReason,
  type SmartRoutingUnavailableCause,
} from "@/lib/smartRoutingAvailability";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { partitionAgentsByKind, sortAgentsForDisplay } from "@/lib/agentGrouping";
import { cn } from "@/lib/utils";
import { isCurrentServerLocal } from "@/lib/serverOrigin";
import {
  isFullySupportedNativeCodingAgent,
  isNativeCodingAgent,
  isRecentHarness,
  nativeAgentHasCapability,
  nativeCodingAgentForAvailableAgent,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { useHostModelOptions, useHosts, type Host } from "@/hooks/useHosts";
import {
  controlHost,
  getHostIdentity,
  isElectronShell,
  onHostStatusChanged,
  type HostIdentity,
} from "@/lib/nativeBridge";
import {
  useAvailableAgents,
  prefetchAvailableAgentDetails,
  type AvailableAgent,
} from "@/hooks/useAvailableAgents";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import { useRecentHarnesses } from "@/hooks/useRecentHarnesses";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import { useNativeServerSwitcherForMainSurface } from "@/hooks/useNativeServerSwitcher";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import type { Conversation } from "@/hooks/useConversations";
import type { NativeModelOption } from "@/lib/types";
import {
  useProjectConfig,
  useProjects,
  moveConversationToProject,
  PROJECT_LABEL_KEY,
} from "@/hooks/useConversations";
import {
  collectConversationIds,
  type ConversationsInfiniteData,
  type SessionListWireItem,
} from "@/lib/sessionListCache";
import { nextPushedSession } from "@/lib/sessionUpdatesSocket";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import { useMentionBrowser } from "@/hooks/useMentionBrowser";
import {
  buildMentionPreamble,
  detectMentionAt,
  mentionItemPath,
  type MentionState,
  parseMentionToken,
  rankMentionEntries,
} from "@/lib/composerMentions";
import { OttoEyes } from "@/components/OttoEyes";
import { SkillPills } from "@/components/SkillPills";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import type { CostControlMode } from "@/components/CostRoutingControl";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentRowTooltip } from "@/components/AgentHoverCard";
import { CreateAgentDialog } from "./CreateAgentDialog";
import { buildAgentBundle, type AgentBundleInput } from "@/lib/agentBundle";
import { createBundledSession, launchRunner } from "@/lib/sessionsApi";

// Hidden from the new-session picker only. `nessie` is superseded by polly.
// `kimi` / `kimi-code` are the headless SDK harness (kept for sub-agent / `run
// --harness kimi` use) — the picker offers only the native TUI (`kimi-native-ui`).
const NEW_SESSION_HIDDEN_AGENTS = new Set(["nessie", "kimi", "kimi-code"]);

// Short picker-row blurbs — the spec descriptions are long paragraphs that
// truncate badly in the dropdown; other dialogs keep the server values.
const AGENT_PICKER_DESCRIPTIONS: Record<string, string> = {
  polly: "Multi-agent coding",
  debby: "Multi-agent debate",
};

// Agents whose bundled skills render as always-visible pills under the
// landing composer. Deliberately an allowlist while the pattern proves
// out — other agents keep the "/" menu as the only skill surface.
const SKILL_PILL_AGENTS = new Set(["polly", "debby"]);

// Claude Code's `claude --permission-mode` choices (v2.1). Claude-native
// sessions only. "default" is Claude's own default and sends no flag; any
// other value is passed through as `--permission-mode <value>` via the
// session's terminal_launch_args. Keep in sync with `claude --help`.
const CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE = "default";
const CLAUDE_NATIVE_PERMISSION_MODES: { value: string; label: string; description: string }[] = [
  { value: "default", label: "Default", description: "Prompts before edits and commands" },
  {
    value: "auto",
    label: "Auto",
    description: "Auto-runs; a classifier blocks risky actions",
  },
  {
    value: "acceptEdits",
    label: "Accept edits",
    description: "Auto-applies file edits; commands still prompt",
  },
  { value: "plan", label: "Plan", description: "Plans only; makes no edits" },
  { value: "dontAsk", label: "Don't ask", description: "Auto-denies anything not pre-approved" },
  {
    value: "bypassPermissions",
    label: "Bypass permissions",
    description: "Runs everything; no prompts or safety checks",
  },
];

// The Auto Harness's Permissions vocabulary: Default only. No cross-harness
// permission mapping exists, so the row stays locked and the create call sends
// no override — each CLI keeps the machine's own configuration.
const AUTO_PERMISSION_MODE = {
  value: CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  label: "Default",
  description: "The picked harness keeps its own configured permissions",
} as const;
const AUTO_PERMISSION_MODE_OPTIONS = [AUTO_PERMISSION_MODE] as const;

// Cursor execution modes. "default" sends no flags; other values map to CLI
// args passed via terminal_launch_args. Keep in sync with `cursor-agent --help`.
const CURSOR_NATIVE_DEFAULT_EXEC_MODE = "default";
const CURSOR_NATIVE_EXEC_MODES: {
  value: string;
  label: string;
  description: string;
  args: string[];
}[] = [
  {
    value: "default",
    label: "Default",
    description: "Normal agent mode; prompts before running commands",
    args: [],
  },
  {
    value: "auto-review",
    label: "Auto-review",
    description: "Smart Auto: auto-runs safe tool calls and prompts for the rest",
    args: ["--auto-review"],
  },
  {
    value: "plan",
    label: "Plan",
    description: "Read-only planning; analyzes and proposes plans, no edits",
    args: ["--mode", "plan"],
  },
  {
    value: "ask",
    label: "Ask",
    description: "Q&A style; explains and answers questions (read-only)",
    args: ["--mode", "ask"],
  },
  {
    value: "yolo",
    label: "Yolo",
    description: "Runs everything without prompts or safety checks",
    args: ["--yolo"],
  },
];

// Codex approval presets matching the `/permissions` TUI popup.
// Each preset bundles a sandbox profile + approval policy, mirroring
// codex-rs/utils/approval-presets/src/lib.rs. "default" is the auto
// preset (workspace-write + on-request) and sends no flags so the
// runner uses Codex's built-in default.
// Keep in sync with `codex --help` and
// https://developers.openai.com/codex/agent-approvals-security
const CODEX_NATIVE_DEFAULT_APPROVAL_MODE = "default";
const CODEX_NATIVE_APPROVAL_MODES: {
  value: string;
  label: string;
  description: string;
  args: string[];
}[] = [
  {
    value: "default",
    label: "Default",
    description: "Read/edit/run in workspace; approval for external edits or network",
    args: [],
  },
  {
    value: "full-access",
    label: "Full access",
    description: "Edit any file and access the internet without approval",
    args: ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
  },
  {
    value: "read-only",
    label: "Read only",
    description: "Read files only; approval required for edits, commands, or network",
    args: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
  },
];

// Conversation-label key for the DANGEROUS codex full-bypass opt-in. When
// set to "1" the runner launches Codex with
// `--dangerously-bypass-approvals-and-sandbox` (no approval prompts, no
// command sandbox) — see omnigent.stores.conversation_store
// CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY. Stored as a label (cheap thread
// metadata) so it survives reload. Mutually exclusive in spirit with the
// approval-mode presets above: when bypass is on the runner strips any
// `--sandbox` / `--ask-for-approval` flags those presets would emit.
const CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox";
// Bypass is the most-permissive Codex approval stance — presented as a 4th
// option in the Codex approval dropdown (Codex only; OpenCode shares the
// presets above but has no bypass). It rides as a conversation label, not
// terminal_launch_args, so its `args` are empty and it's handled specially.
const CODEX_NATIVE_BYPASS_APPROVAL_VALUE = "bypass";
const CODEX_NATIVE_BYPASS_APPROVAL_OPTION = {
  value: CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  label: "Bypass approvals & sandbox",
  description: "Runs Codex with no approval prompts and no command sandbox",
  args: [] as string[],
};

function displayModelId(option: Pick<NativeModelOption, "id">): string {
  return option.id;
}

function displayModelName(option: Pick<NativeModelOption, "id" | "displayName">): string {
  return option.displayName ?? option.id;
}

function defaultModelLabel(
  options: readonly Pick<NativeModelOption, "id" | "displayName" | "isDefault">[],
  display: (option: Pick<NativeModelOption, "id" | "displayName">) => string,
): string {
  const dflt = options.find((option) => option.isDefault);
  return dflt ? `Default (${display(dflt)})` : "Default";
}

/** Use a local-friendly label only when the desktop shell proves the host id is this machine. */
export function displayNameForHost(
  host: Pick<Host, "host_id" | "name">,
  thisMachineHostId: string | null,
  userAgent: string,
): string {
  if (thisMachineHostId === null || host.host_id !== thisMachineHostId) return host.name;
  if (/iPhone/i.test(userAgent)) return "This iPhone";
  if (/iPad/i.test(userAgent)) return "This iPad";
  if (/Android/i.test(userAgent)) return "This Android";
  if (/Windows/i.test(userAgent)) return "This Windows";
  if (/Macintosh|Mac OS X/i.test(userAgent)) return "This Mac";
  if (/Linux|X11/i.test(userAgent)) return "This machine";
  return host.name;
}

/** Resolve this machine exactly from Electron, or conservatively from a local single-host server. */
export function resolveThisMachineHostId(
  desktopHostId: string | null,
  serverIsLocal: boolean,
  onlineHostIds: readonly string[],
): string | null {
  if (desktopHostId !== null) return desktopHostId;
  return serverIsLocal && onlineHostIds.length === 1 ? onlineHostIds[0] : null;
}

function HostOption({
  host,
  displayName = host.name,
  subtitle,
}: {
  host: Host;
  displayName?: string;
  subtitle?: string;
}) {
  const isOnline = host.status === "online";
  return (
    <span className="flex min-w-0 items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 shrink-0 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 shrink-0 text-muted-foreground" />
      )}
      <span className="flex min-w-0 flex-col">
        <span className="flex items-center gap-2">
          <span className="truncate text-sm">{displayName}</span>
          <span
            className={`inline-flex shrink-0 items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${isOnline ? "text-green-600" : "text-muted-foreground"}`}
          >
            <span
              className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
            />
            {host.status}
          </span>
        </span>
        {subtitle && (
          <span className="text-[10px] leading-tight text-muted-foreground">{subtitle}</span>
        )}
      </span>
    </span>
  );
}

export function ConnectHostInstructions({
  serverUrl,
  label,
}: {
  serverUrl: string;
  label?: string;
}) {
  // Databricks/internal deployments add the "Databricks Lakebox" connect
  // path; OSS deployments (where the lakebox launcher is excluded) show
  // only the plain `omni host` command. Driven by /v1/info.
  const info = useServerInfo();
  // "loading" before the boot probe resolves → treat as OSS (no Databricks
  // hints) until known, so the clean UI shows first and lakebox never flashes.
  const databricksFeatures = info !== "loading" && info.databricks_features;
  return (
    <div className="flex flex-col gap-4 rounded-lg border border-dashed border-border p-4">
      {label && <p className="text-sm text-muted-foreground">{label}</p>}
      {databricksFeatures ? (
        <Tabs defaultValue="local">
          <TabsList className="w-full">
            <TabsTrigger value="local" className="text-sm">
              Local machine
            </TabsTrigger>
            <TabsTrigger value="lakebox" className="text-sm">
              Databricks Lakebox
            </TabsTrigger>
          </TabsList>
          <TabsContent value="local">
            <CliCommandBlock
              command={`omni host --server ${serverUrl}`}
              testIdPrefix="connect-host"
            />
          </TabsContent>
          <TabsContent value="lakebox" className="flex flex-col gap-1.5">
            <CliCommandBlock
              command="omni sandbox create --provider lakebox"
              testIdPrefix="connect-lakebox-create"
            />
            <CliCommandBlock
              command={`omni sandbox connect --provider lakebox --sandbox-id <id> --server ${serverUrl}`}
              testIdPrefix="connect-lakebox-connect"
            />
          </TabsContent>
        </Tabs>
      ) : (
        <CliCommandBlock command={`omni host --server ${serverUrl}`} testIdPrefix="connect-host" />
      )}
    </div>
  );
}

/**
 * Return true when ``workspace`` is acceptable to send to the backend.
 *
 * Per designs/SESSION_WORKSPACE_SELECTION.md: only fully-absolute
 * paths (starting with ``/``) are accepted. Tilde-prefixed and
 * relative paths are rejected because the server never expands ``~``
 * — that's the host's job, and the workspace request body must be
 * an unambiguous absolute path. Empty / whitespace-only input is
 * also rejected so the submit button is disabled until the user
 * has typed something usable.
 *
 * @param workspace Value the user typed in the workspace input.
 * @returns true when ``workspace.trim()`` starts with ``/``.
 */
export function isValidWorkspace(workspace: string): boolean {
  return workspace.trim().startsWith("/");
}

/**
 * Normalize a host filesystem path for equality comparison.
 *
 * Trims whitespace and strips trailing slashes so ``"/repo/"`` and
 * ``"/repo"`` compare equal, preserving the root ``"/"``. Blank/whitespace
 * input returns ``null`` (no path), never the root. Lexical only — no ``..``
 * or symlink resolution — which suffices because the server stores canonical
 * absolute workspaces, so a freshly typed absolute path matches directly.
 *
 * @param path A host path, e.g. ``"/Users/me/repo/"``.
 * @returns The normalized path, e.g. ``"/Users/me/repo"``; ``null`` for blank.
 */
export function normalizeWorkspacePath(path: string): string | null {
  const trimmed = path.trim();
  if (trimmed === "") return null;
  const stripped = trimmed.replace(/\/+$/, "");
  // All-slashes input (e.g. "///") collapses to the root.
  return stripped === "" ? "/" : stripped;
}

/**
 * Shorten an absolute path to its last two segments with a leading
 * ellipsis, so worktree rows show the disambiguating tail (e.g.
 * ``"…/myrepo-worktrees/feature-x"``) instead of a shared prefix that
 * truncates to the same string for every entry.
 *
 * @param path Absolute path, e.g. ``"/Users/me/myrepo-worktrees/feature-x"``.
 * @returns The tail, prefixed with ``"…/"`` when segments were dropped;
 *   the original path when it already has two or fewer segments.
 */
export function worktreePathTail(path: string): string {
  const segments = path.replace(/\/+$/, "").split("/").filter(Boolean);
  if (segments.length <= 2) return path;
  return `…/${segments.slice(-2).join("/")}`;
}

/**
 * Existing sessions that would share an on-disk working directory with a new
 * session created in ``workspace`` on ``hostId``.
 *
 * Matches on host plus normalized workspace path: a session whose stored
 * ``workspace`` equals the picked directory works in that same directory.
 * Branch sessions live in isolated worktree dirs (a different ``workspace``),
 * so they only match when the user explicitly picked that worktree path.
 *
 * Only *connected* sessions count — ``isRunnerOnline(s.id)`` must hold. An
 * offline or unbound session has no live process that could write the
 * directory, so it isn't a conflict. The caller backs this predicate with
 * the shared runner-health poll — the same ``/health`` signal as the
 * sidebar's connectivity dots — so the hint agrees with what the sidebar
 * shows.
 * Deleted sessions (≈ openui's archived) are already filtered out
 * server-side. An errored (``failed``) session whose runner is still online
 * counts, mirroring openui: only *disconnected* agents are excluded, not
 * merely errored ones.
 *
 * Returns ``[]`` when ``hostId`` is unset or ``workspace`` is blank.
 *
 * @param sessions The caller's sessions from ``useDirectorySessions``.
 * @param hostId The selected host id, or ``null`` when none is picked.
 * @param workspace The picked absolute directory, e.g. ``"/Users/me/repo"``.
 * @param isRunnerOnline Predicate: is this session's runner online right now?
 *   Backed by the shared runner-health poll in the component.
 * @returns Matching connected sessions; callers use ``.length`` for the count.
 */
export function sessionsSharingDirectory(
  sessions: Conversation[],
  hostId: string | null,
  workspace: string,
  isRunnerOnline: (sessionId: string) => boolean,
): Conversation[] {
  if (!hostId) return [];
  const target = normalizeWorkspacePath(workspace);
  if (target === null) return [];
  // TODO: headless agents (no `os_env`, no filesystem access) still get a
  // workspace via the web flow, so they count here — a false positive, since
  // they can't write. SessionListItem doesn't expose filesystem capability to
  // filter on; revisit (expose a flag + skip them) if headless agents with
  // working directories become common.
  return sessions.filter(
    (s) =>
      s.host_id === hostId &&
      s.workspace != null &&
      normalizeWorkspacePath(s.workspace) === target &&
      // Only a session whose runner is actually online has a live process
      // that could write here — same connectivity signal as the sidebar.
      isRunnerOnline(s.id),
  );
}

/**
 * Best-effort human-readable message for a failed POST /v1/sessions.
 *
 * Recognizes the OmnigentError shape (``{error: {message}}``) and
 * FastAPI's ``{detail}``; falls back to the status code otherwise.
 *
 * @param res Non-OK response from the session-create call.
 * @returns A message to show the user; falls back to the status code
 *   when the body isn't a recognizable error shape.
 */
export async function describeCreateError(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      // FastAPI HTTPException → {detail}; OpenResponses → {error:{message}}.
      const b = body as Record<string, unknown>;
      if (typeof b.detail === "string") return b.detail;
      if (
        Array.isArray(b.detail) &&
        b.detail.length > 0 &&
        typeof (b.detail[0] as Record<string, unknown>)?.msg === "string"
      ) {
        return (b.detail[0] as Record<string, unknown>).msg as string;
      }
      if (typeof b.message === "string") return b.message;
      const err = b.error;
      if (typeof err === "string") return err;
      if (
        err &&
        typeof err === "object" &&
        typeof (err as Record<string, unknown>).message === "string"
      ) {
        return (err as Record<string, unknown>).message as string;
      }
    }
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return `Couldn't create the session (HTTP ${res.status}).`;
}

/**
 * The pre-feature "run omni setup" guidance (ReactNode), shown under the
 * composer when the UI-driven setup feature is OFF.
 *
 * The ``needs-auth`` / ``binary-missing`` copy is Codex-specific ("run codex
 * login" / "set OMNIGENT_CODEX_PATH"), so it's gated on {@link isCodexHarness}.
 * Other harnesses that report those structured reasons (claude-native /
 * opencode-native now do) fall through to the generic "run omni setup"
 * message — matching the pre-feature behavior, where only Codex ever produced
 * these reasons and everything else showed the generic text.
 */
function harnessWarningMessage(
  agentName: string | undefined,
  hostName: string | undefined,
  reason: string | null,
  harness: string | null | undefined,
): ReactNode {
  const isCodex = !!harness && isCodexHarness(harness);
  if (reason === "needs-auth" && isCodex) {
    return (
      <>
        {agentName} needs Codex authentication on {hostName} — run <code>codex login</code> on that
        machine.
      </>
    );
  }
  if (reason === "needs-auth" && !!harness && isNativeCursorHarness(harness)) {
    return (
      <>
        {agentName} needs Cursor login on {hostName} — run <code>cursor-agent login</code> on that
        machine.
      </>
    );
  }
  // ``version-too-low`` is a uniform state across all CLI harnesses now that
  // the server checks supported version ranges. Keep the message generic so
  // the user is nudged toward setup rather than being told the CLI is missing.
  if (reason === "version-too-low") {
    return (
      <>
        {agentName} has an outdated CLI on {hostName} — run <code>omni setup</code>, or upgrade the
        CLI directly on that machine.
      </>
    );
  }
  return (
    <>
      {agentName} isn&apos;t configured on {hostName} — run <code>omni setup</code> on that machine.
    </>
  );
}

/**
 * Amber "harness not ready on this host" notice under the composer, for the
 * currently-selected agent (case A: surfaced without opening the picker).
 *
 * Gated on the setup feature: when OFF, renders the original "run omnigent
 * setup" guidance so the flag-off UI is unchanged. When ON, offers a "Set up
 * <agent>" action that opens the shared {@link HarnessSetupDialog}.
 */
function HarnessSetupNotice({
  agentName,
  hostName,
  harness,
  reason,
  featureEnabled,
  onSetup,
}: {
  agentName: string | undefined;
  hostName: string | undefined;
  harness: string | null | undefined;
  reason: string | null;
  featureEnabled: boolean;
  onSetup: () => void;
}) {
  return (
    <p
      // pl-2 lines the icon up with the chips tray directly above (which has
      // pl-2), so the notice reads as part of the composer, not indented left.
      className="flex items-center gap-2 pl-2 text-sm text-amber-600 dark:text-amber-500"
      data-testid="new-chat-landing-harness-warning"
    >
      <TriangleAlertIcon className="size-3.5 shrink-0" />
      {featureEnabled ? (
        <>
          <span>
            {agentName} isn&apos;t ready on {hostName}.
          </span>
          {/* Compact bordered chip — small enough to sit on the sentence's line
              (h-5, text-sm), so it reads as part of the notice. */}
          <button
            type="button"
            data-testid="new-chat-landing-harness-setup"
            className="inline-flex h-5 shrink-0 items-center rounded-md border border-amber-300 px-2 text-sm font-medium text-amber-700 hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 dark:border-amber-500/40 dark:text-amber-400 dark:hover:bg-amber-500/20"
            onClick={onSetup}
          >
            Set up {agentName}
          </button>
        </>
      ) : (
        <span>{harnessWarningMessage(agentName, hostName, reason, harness)}</span>
      )}
    </p>
  );
}

/**
 * Sanitize a user-typed initial prompt before it is sent.
 *
 * Strips C0/C1 control characters that could corrupt a terminal
 * agent's input when the runner injects the text via ``tmux
 * send-keys`` (Claude Code / Codex native), while preserving newlines
 * (``\n``) and tabs (``\t``) so multi-line prompts survive. Mirrors
 * openui's server-side terminal-input sanitization. Trailing/leading
 * whitespace is trimmed so a whitespace-only prompt collapses to "".
 *
 * @param prompt Raw textarea value the user typed, e.g.
 *   ``"read the README\nand summarize"``.
 * @returns The sanitized prompt; ``""`` when there's nothing to send.
 */
export function sanitizeInitialPrompt(prompt: string): string {
  // Intentional control-char class: strips C0 (\x00-\x1f) and C1
  // (\x7f-\x9f) ranges EXCEPT \t (\x09) and \n (\x0a), which multi-line
  // prompts need. The control chars in the class are the point of the
  // rule, so suppress no-control-regex here (oxlint honors this).
  // eslint-disable-next-line no-control-regex
  return prompt.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "").trim();
}

/**
 * Return true when ``url`` is acceptable as a sandbox repository URL.
 *
 * Mirrors the server's accepted forms (``parse_repo_workspace``):
 * ``https://<host>/<path>`` or scp-style ``git@<host>:<path>``. The
 * server is the authority — this only gates the submit button so an
 * obviously unusable value gets inline feedback instead of a 422.
 *
 * @param url Value the user typed in the repository input.
 * @returns true when ``url.trim()`` matches one of the two forms.
 */
export function isValidSandboxRepoUrl(url: string): boolean {
  const t = url.trim();
  return /^https:\/\/[^\s#/]+\/[^\s#]+$/.test(t) || /^git@[^\s#:]+:[^\s#]+$/.test(t);
}

/**
 * Compose the managed session's ``workspace`` string from the split
 * repository inputs.
 *
 * The API takes one Docker-build-context-style string —
 * ``<url>[#<branch>]`` — and the UI presents split fields, so this is
 * the reassembly step.
 *
 * @param url Repository URL input, e.g. ``"https://github.com/org/repo"``.
 * @param branch Branch input, e.g. ``"main"``; blank means the repo's
 *   default branch.
 * @returns The composed workspace string, or ``undefined`` when no
 *   repository was given (empty sandbox workspace).
 */
export function composeSandboxWorkspace(url: string, branch: string): string | undefined {
  const u = url.trim();
  if (u === "") return undefined;
  const b = branch.trim();
  return b === "" ? u : `${u}#${b}`;
}

/**
 * Derive a repository's display name from its URL.
 *
 * Last path segment with a trailing ``.git`` stripped — the same rule
 * the server uses for the clone directory, so the chip label matches
 * the workspace directory the session will get.
 *
 * @param url Repository URL, e.g. ``"https://github.com/org/repo.git"``.
 * @returns The name, e.g. ``"repo"``; ``null`` when underivable.
 */
export function deriveRepoName(url: string): string | null {
  const t = url.trim().replace(/\/+$/, "");
  if (t === "") return null;
  const last = t.split(/[/:]/).pop() ?? "";
  const name = last.endsWith(".git") ? last.slice(0, -4) : last;
  return name === "" ? null : name;
}

/**
 * Match a first message against an agent's bundled skills.
 *
 * Uses the in-session composer's shared command-shape guard
 * (:func:`isSlashCommandText`): the first token must read as ``/name``
 * (file paths like ``/etc/hosts`` never match), while the args after it
 * may carry anything — including paths and URLs, e.g.
 * ``"/review-pr https://github.com/..."``. The command name must
 * exactly match a bundled skill. Anything else — including
 * host-discovered skills the server can't know before a runner boots —
 * is sent as plain text, the same fall-through the in-session composer
 * uses for unknown commands.
 *
 * @param text The sanitized first message, e.g. ``"/review-pr 123"``.
 * @param skills The chosen agent's bundled skills from GET /v1/agents.
 * @returns The skill name and argument string, or ``null`` when the
 *   text is not an invocation of a bundled skill.
 */
export function matchSkillInvocation(
  text: string,
  skills: readonly { name: string }[],
): { name: string; args: string } | null {
  const trimmed = text.trim();
  if (!isSlashCommandText(trimmed)) return null;
  const command = trimmed.split(/\s+/)[0]!;
  const name = command.slice(1);
  if (!skills.some((s) => s.name === name)) return null;
  return { name, args: trimmed.slice(command.length).trim() };
}

/**
 * Derive a host's home directory from a listing of its home contents.
 *
 * The filesystem endpoint returns home's entries with absolute paths (e.g.
 * ``"/Users/you/projects"``), so home is the parent of any entry. Returns
 * ``null`` for an empty listing — a literally empty home dir is the one case
 * this can't resolve, and the caller falls back to a blank field (the picker
 * still opens straight onto home).
 *
 * @param entries Entries from listing the host's home directory.
 * @returns The home directory path, or ``null`` when it can't be derived.
 */
export function deriveHomeDir(entries: HostFilesystemEntry[]): string | null {
  const first = entries[0];
  if (!first) return null;
  const slash = first.path.lastIndexOf("/");
  if (slash < 0) return null;
  return slash === 0 ? "/" : first.path.slice(0, slash);
}

/**
 * The home-page ("/") landing composer.
 *
 * Owns session creation end-to-end: the textarea is the first message and the
 * configuration chips (host, working directory, git worktree) plus the agent
 * picker supply every required parameter. Hitting send POSTs /v1/sessions and
 * navigates to the new session — there is no modal.
 */
/** Group / section header inside the picker dropdown (plain div, so Radix
 * doesn't claim roving focus for it — mirrors the in-session picker). */
function PickerSectionHeader({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 pt-1.5 pb-0.5 text-sm font-medium text-muted-foreground">{children}</div>
  );
}

/**
 * Unified two-level agent/harness picker for the landing composer.
 *
 * **Level 1** groups every available agent under "Agents" (SDK / bundle
 * agents like Polly & Debby, plus custom user agents) and "Harnesses" (the
 * native terminal CLIs — Claude Code, Codex, Cursor, …). **Level 2** is a
 * per-entry submenu of that entry's run-config knobs: model / effort /
 * permission mode for Claude Code, approval mode (+ bypass) for Codex,
 * approval mode for OpenCode, execution mode for Cursor, and the brain-harness
 * override for bundle agents. Entries with no knobs are plain selectable rows.
 *
 * Holds no state of its own — the selected agent and every knob live in
 * {@link NewChatLandingScreen} and are threaded in. Replaces the old
 * left-side run-mode pills, the right-side model / harness controls, and the
 * footer-tray agent dropdown.
 *
 * Selecting a knob inside a not-yet-selected entry's submenu first selects
 * that entry (so the single shared knob state stays coherent), then applies
 * the value. For the mode knobs we persist the pick for the *entry's* harness
 * BEFORE selecting, so the harness-switch reseed effect in the screen reads it
 * back as the same value and doesn't clobber the choice.
 */
export function AgentHarnessPicker({
  agentEntries,
  harnessEntries,
  effectiveAgentId,
  agentLabel,
  hasAgents,
  host,
  onSelectAgent,
  pendingAgent,
  pendingAgentId,
  onSelectPending,
  onCreateCustomAgent,
  sandboxSelected,
  allowCreateCustomAgent = true,
  onOpenChange,
  dropdownModal = true,
  contentClassName,
  contentAlign = "end",
  triggerClassName,
  triggerLabelClassName,
  triggerTooltip,
  autoHarnessAvailable = false,
  autoHarnessActive = false,
  onSelectAutoHarness,
}: {
  agentEntries: AvailableAgent[];
  harnessEntries: AvailableAgent[];
  effectiveAgentId: string | null;
  agentLabel: string;
  hasAgents: boolean;
  host: Host | undefined | null;
  onSelectAgent: (agent: AvailableAgent) => void;
  pendingAgent: AgentBundleInput | null;
  pendingAgentId: string;
  onSelectPending: () => void;
  onCreateCustomAgent: () => void;
  sandboxSelected: boolean;
  /** Whether to offer the "Create custom agent" action. Defaults true; an
   *  embedder that only picks an existing agent (e.g. project settings) can
   *  hide it since it has no interactive create flow. */
  allowCreateCustomAgent?: boolean;
  // ── Optional reuse hooks (all default-undefined) ─────────────────────────
  // These let a host OTHER than the composer footer embed the picker without
  // changing its default behavior. The interactive New Chat call site passes
  // none of them, so it renders exactly as before. The scheduled-task create
  // dialog passes them to: forward the dropdown open/close into its own
  // outside-click dismiss guard (`onOpenChange`), bound + left-align the menu
  // in a tall modal (`contentClassName` / `contentAlign`), and style the
  // trigger to match sibling <Select> fields (`triggerClassName` /
  // `triggerLabelClassName`).
  /** Notified when the picker dropdown opens/closes. */
  onOpenChange?: (open: boolean) => void;
  /** Whether the Radix dropdown should modal-block outside content. Defaults true. */
  dropdownModal?: boolean;
  /** Extra classes merged onto the dropdown content (e.g. a tighter max-h). */
  contentClassName?: string;
  /** Dropdown alignment. Defaults to "end" (composer footer). */
  contentAlign?: "start" | "center" | "end";
  /** Extra classes merged onto the trigger Button. */
  triggerClassName?: string;
  /** Extra classes merged onto the trigger's label span. */
  triggerLabelClassName?: string;
  /** Hover text explaining the current pick, when the label alone doesn't say
   *  what runs (e.g. "Auto"). Omitted → no tooltip, as before. */
  triggerTooltip?: string;
  /** Whether the top-level Smart Routing row is offered (routing enabled and
   *  both native CLIs ready). Defaults off, so an embedder that doesn't wire
   *  routing never shows it. */
  autoHarnessAvailable?: boolean;
  /** Whether Smart Routing is the current pick. It rides a placeholder agent,
   *  so this also suppresses that agent's row highlight — otherwise two rows
   *  would look selected at once. */
  autoHarnessActive?: boolean;
  onSelectAutoHarness?: () => void;
}) {
  // Controlled so picking a row can close the menu.
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const info = useServerInfo();
  // Feature ON → single "needs setup" badge; OFF → per-reason original text.
  const collapsedBadge = info !== "loading" && info.harness_install_enabled;
  const triggerRef = useRef<HTMLButtonElement>(null);

  // Touch devices can't hover, so the desktop submenu flyouts ("More",
  // "Custom agents") are unreachable there. On mobile we swap the dropdown's
  // contents in place: tapping the row drills into that group's page (with a
  // Back row), instead of opening a hover flyout. `mobilePage` is the open
  // group (null = the main list); inert on desktop.
  const isMobile = useIsMobileViewport();
  const [mobilePage, setMobilePage] = useState<"more" | "custom" | null>(null);
  // Reset to the main list whenever the menu closes so it never reopens on a
  // stale drill-in page.
  useEffect(() => {
    if (!open) setMobilePage(null);
  }, [open]);

  // The agent name + optional short blurb, with the full spec description on
  // hover. Run-config knobs now live in the gear-icon config modal, not here —
  // this picker only selects the agent / harness.
  const renderRowInner = (agent: AvailableAgent, withTooltip: boolean) => {
    const blurb = AGENT_PICKER_DESCRIPTIONS[agent.name];
    const inner = (
      <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
        <span className="truncate">{agent.display_name}</span>
        {blurb && <span className="truncate text-sm text-muted-foreground/70">{blurb}</span>}
      </div>
    );
    return withTooltip ? <AgentRowTooltip agent={agent}>{inner}</AgentRowTooltip> : inner;
  };

  const renderBadge = (agent: AvailableAgent) =>
    harnessUnconfiguredOnHost(agent.harness, host) ? (
      <Badge
        variant="outline"
        className="ml-auto self-center border-amber-300 bg-amber-50 text-sm text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
        data-testid={`new-chat-landing-agent-warning-${agent.id}`}
      >
        {harnessWarningBadgeText(
          harnessUnavailableReasonOnHost(agent.harness, host),
          collapsedBadge,
        )}
      </Badge>
    ) : null;

  // Each entry is a plain selectable row — selecting commits the pick and
  // closes the menu. Run-config knobs moved to the gear-icon config modal.
  const renderEntry = (agent: AvailableAgent): ReactNode => {
    // Smart Routing binds a placeholder agent for the create call, so its own
    // row owns the active state while it's picked.
    const active = !autoHarnessActive && agent.id === effectiveAgentId;
    return (
      <DropdownMenuItem
        key={agent.id}
        data-testid={`new-chat-landing-agent-${agent.id}`}
        data-active={active ? "true" : undefined}
        onSelect={() => onSelectAgent(agent)}
        className="items-start data-[active=true]:bg-muted data-[active=true]:text-foreground dark:data-[active=true]:bg-muted/50"
      >
        {renderRowInner(agent, true)}
        {renderBadge(agent)}
      </DropdownMenuItem>
    );
  };

  // Opt-in "hide unconfigured harnesses" filter (Settings › Appearance). When
  // on, drop harness rows that can't launch on the selected host. Fails open:
  // harnessUnconfiguredOnHost returns false with no host / no readiness map, so
  // nothing is hidden in those cases, and unrecognized harnesses stay visible.
  const hideUnconfigured = useMemo(() => readHideUnconfiguredHarnesses(), []);
  const { recentHarnesses } = useRecentHarnesses();
  // Split harnesses by support level: the fully supported ones lead the primary
  // list, and every other harness folds into "More" whether or not it is
  // configured here. Also promoted out of "More": the selected harness (never
  // bury the active pick) and any the user has launched before, so a regular
  // Pi / Cursor user gets theirs one click away instead of one hover.
  const { readyHarnessEntries, moreHarnessEntries } = useMemo(() => {
    const ready: AvailableAgent[] = [];
    const more: AvailableAgent[] = [];
    for (const a of harnessEntries) {
      const selected = a.id === effectiveAgentId;
      // The preference hides harnesses that can't launch here — it outranks
      // both support level and recency, but never buries the active pick.
      if (!selected && hideUnconfigured && harnessUnconfiguredOnHost(a.harness, host)) continue;
      if (selected || isFullySupportedNativeCodingAgent(a) || isRecentHarness(a, recentHarnesses)) {
        ready.push(a);
      } else more.push(a);
    }
    return { readyHarnessEntries: ready, moreHarnessEntries: more };
  }, [harnessEntries, host, hideUnconfigured, effectiveAgentId, recentHarnesses]);

  // Split the agents group: built-in bundle agents (Polly / Debby) stay inline
  // in the main list; user-registered custom agents fold into a "Custom agents"
  // submenu so a long roster doesn't crowd out the recommended picks.
  const { builtins: bundleEntries, customs: customEntries } = useMemo(
    () => partitionAgentsByKind(agentEntries),
    [agentEntries],
  );

  // Existing custom / pending agents fold into a "Custom agents" submenu so a
  // long roster doesn't crowd the recommended picks. When there are none, the
  // submenu would hold only the create action — which is a poor place to
  // discover it — so we surface "Create custom agent" as a top-level row
  // instead (see below). The submenu therefore renders only when there is at
  // least one custom / pending agent to group.
  const hasCustomAgents = customEntries.length > 0 || pendingAgent != null;
  // "Create custom agent" is reachable on any non-sandbox target (a managed
  // sandbox has no create path for an uploaded bundle), unless the embedder
  // opts out (it has no create flow to route the action to).
  const canCreateAgent = !sandboxSelected && allowCreateCustomAgent;
  const createAgentItem = canCreateAgent ? (
    <DropdownMenuItem
      data-testid="new-chat-landing-create-agent"
      onSelect={onCreateCustomAgent}
      className="text-muted-foreground"
    >
      <PlusIcon className="size-3.5" />
      Create custom agent
    </DropdownMenuItem>
  ) : null;
  const hasCustomGroup = hasCustomAgents;
  // Shared body for the custom-agents submenu (desktop flyout + mobile page):
  // the custom agents, the pending upload, and the create action.
  const customAgentsBody = (
    <>
      {customEntries.map(renderEntry)}
      {pendingAgent && (
        <DropdownMenuItem
          key={pendingAgentId}
          data-testid="new-chat-landing-agent-pending"
          data-active={effectiveAgentId === pendingAgentId ? "true" : undefined}
          onSelect={onSelectPending}
          className="items-start data-[active=true]:bg-muted data-[active=true]:text-foreground dark:data-[active=true]:bg-muted/50"
        >
          <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
            <span className="truncate">{pendingAgent.name}</span>
            <span className="truncate text-sm text-muted-foreground/70">Custom</span>
          </div>
        </DropdownMenuItem>
      )}
      {canCreateAgent && (
        <>
          <DropdownMenuSeparator />
          {createAgentItem}
        </>
      )}
    </>
  );
  // Which mobile drill-in page is showing (gated so a group that vanished — e.g.
  // list refresh — can't strand the menu on an empty page).
  const showMore = isMobile && mobilePage === "more" && moreHarnessEntries.length > 0;
  const showCustom = isMobile && mobilePage === "custom" && hasCustomGroup;
  // If the open page's group disappears (or the viewport grows to desktop),
  // fall back to the main list so a reopened menu never lands on an empty page.
  useEffect(() => {
    if (mobilePage === "more" && !showMore) setMobilePage(null);
    if (mobilePage === "custom" && !showCustom) setMobilePage(null);
  }, [mobilePage, showMore, showCustom]);

  return (
    <DropdownMenu
      modal={dropdownModal}
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        onOpenChange?.(next);
        if (next) {
          // Prefetch harness/description/skills for all session-discovered
          // agents so the list is stable before the user reads it.
          for (const agent of [...harnessEntries, ...agentEntries]) {
            void prefetchAvailableAgentDetails(agent, queryClient);
          }
        }
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          ref={triggerRef}
          type="button"
          variant="ghost"
          size="sm"
          disabled={!hasAgents}
          data-testid="new-chat-landing-agent-select"
          title={triggerTooltip}
          // Drop the Button's focus-visible ring/border that otherwise shows
          // when focus returns to the trigger after a pick. `triggerClassName`
          // (default undefined) lets an embedder override sizing/border to match
          // its own form fields; tailwind-merge lets the passed classes win.
          className={cn(
            "h-8 gap-1.5 pr-1 pl-2.5 font-normal text-muted-foreground hover:text-foreground focus-visible:border-transparent focus-visible:ring-0",
            triggerClassName,
          )}
        >
          <span
            className={cn("max-w-[12rem] truncate text-ui text-foreground", triggerLabelClassName)}
          >
            {hasAgents ? agentLabel : "No agents"}
          </span>
          <ChevronDownIcon className="size-3.5 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align={contentAlign}
        // Keep the menu inside the viewport on short mobile screens: pad the
        // collision box so the available-height cap leaves room below the
        // status bar, and let it flip/scroll rather than run off the top.
        collisionPadding={12}
        avoidCollisions
        // `contentClassName` (default undefined) lets an embedder tighten the
        // height cap / pin a width; tailwind-merge lets the passed max-h/width
        // override the defaults.
        className={cn(
          "max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-64 max-w-[calc(100vw-2rem)] overflow-y-auto",
          contentClassName,
        )}
      >
        {showMore ? (
          // Mobile drill-in page for the "needs setup" harnesses.
          <div className="animate-in fade-in-0 slide-in-from-right-2 duration-150">
            <DropdownMenuItem
              data-testid="new-chat-landing-page-back"
              onSelect={(e) => {
                e.preventDefault();
                setMobilePage(null);
              }}
              className="items-center font-medium"
            >
              <ChevronLeftIcon className="size-4 shrink-0 opacity-70" />
              <span className="truncate">More</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {moreHarnessEntries.map(renderEntry)}
          </div>
        ) : showCustom ? (
          // Mobile drill-in page for custom agents.
          <div className="animate-in fade-in-0 slide-in-from-right-2 duration-150">
            <DropdownMenuItem
              data-testid="new-chat-landing-page-back"
              onSelect={(e) => {
                e.preventDefault();
                setMobilePage(null);
              }}
              className="items-center font-medium"
            >
              <ChevronLeftIcon className="size-4 shrink-0 opacity-70" />
              <span className="truncate">Custom agents</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {customAgentsBody}
          </div>
        ) : (
          <>
            {/* Smart Routing sits in its own unlabeled group above the
            harnesses: it routes over them rather than being one of them. */}
            {autoHarnessAvailable && (
              <>
                <DropdownMenuItem
                  data-testid="new-chat-landing-harness-smart-routing"
                  data-active={autoHarnessActive ? "true" : undefined}
                  onSelect={() => {
                    onSelectAutoHarness?.();
                    setOpen(false);
                  }}
                  className="items-center text-13 data-[active=true]:bg-muted data-[active=true]:text-foreground dark:data-[active=true]:bg-muted/50"
                >
                  <span className="flex-1 truncate">{SMART_ROUTING_LABEL}</span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
              </>
            )}
            {/* Harnesses group — the native terminal CLIs (Claude Code is the
            default), so the most-used picks lead. Ready-to-use harnesses list
            inline; "needs setup" ones fold into a "More" group. */}
            {(readyHarnessEntries.length > 0 || moreHarnessEntries.length > 0) && (
              <>
                <PickerSectionHeader>Harnesses</PickerSectionHeader>
                {readyHarnessEntries.map(renderEntry)}
                {moreHarnessEntries.length > 0 &&
                  (isMobile ? (
                    // Touch: drill into a "More" page in place (with Back).
                    <DropdownMenuItem
                      data-testid="new-chat-landing-harness-more"
                      onSelect={(e) => {
                        e.preventDefault();
                        setMobilePage("more");
                      }}
                      className="items-center"
                    >
                      <span className="flex-1">More</span>
                      <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground/70" />
                    </DropdownMenuItem>
                  ) : (
                    // Desktop: hover flyout submenu.
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger
                        data-testid="new-chat-landing-harness-more"
                        className="cursor-pointer items-center"
                      >
                        <span className="flex-1">More</span>
                      </DropdownMenuSubTrigger>
                      <DropdownMenuSubContent className="max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-56 max-w-[calc(100vw-2rem)] overflow-y-auto">
                        {moreHarnessEntries.map(renderEntry)}
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                  ))}
                <DropdownMenuSeparator />
              </>
            )}
            {/* Agents group — built-in bundle agents (Polly / Debby) inline. */}
            <PickerSectionHeader>Agents</PickerSectionHeader>
            {bundleEntries.map(renderEntry)}
            {/* Existing custom agents fold into a "Custom agents" submenu (with
            the pending upload and the create action). With no custom agents the
            submenu would hold only "Create custom agent", so we surface that as
            a top-level row instead — otherwise creation is invisible on a fresh
            server. A managed sandbox has no create path, so neither appears. */}
            {hasCustomGroup &&
              (isMobile ? (
                // Touch: drill into a "Custom agents" page in place (with Back).
                <DropdownMenuItem
                  data-testid="new-chat-landing-custom-agents"
                  onSelect={(e) => {
                    e.preventDefault();
                    setMobilePage("custom");
                  }}
                  className="items-center"
                >
                  <span className="flex-1">Custom agents</span>
                  <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground/70" />
                </DropdownMenuItem>
              ) : (
                // Desktop: hover flyout submenu.
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger
                    data-testid="new-chat-landing-custom-agents"
                    className="cursor-pointer items-center"
                  >
                    <span className="flex-1">Custom agents</span>
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-56 max-w-[calc(100vw-2rem)] overflow-y-auto">
                    {customAgentsBody}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              ))}
            {/* No custom agents to group: surface the create action directly so
            it stays discoverable instead of hiding behind an empty submenu. */}
            {!hasCustomGroup && createAgentItem}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Harness-configuration modal opened from the composer's gear icon. Shows the
 * selected agent's run-config knobs — Claude: model / effort / permissions;
 * Codex/OpenCode: approval mode (+ Codex's dangerous full-bypass opt-in);
 * Cursor: exec mode; bundle agents: brain-harness override. On the fully-auto
 * harness the router owns harness and model, so every harness-specific knob
 * drops out — a bundle agent keeps its brain-harness row (the pick lives there
 * and is how the user switches away) plus a locked Permissions row.
 *
 * The modal edits a LOCAL draft seeded from the live state each time it opens,
 * and only commits to the parent state + per-harness persistence on Save;
 * Cancel / dismiss discards. This is the deliberate Save/Cancel UX (the old
 * in-dropdown submenu committed on every change).
 */
function HarnessConfigModal({
  open,
  onOpenChange,
  agent,
  brainHarnessLabels,
  host,
  hideUnconfigured,
  smartRoutingEligible,
  permissionMode,
  approvalMode,
  cursorExecMode,
  bypassSandbox,
  pickedModel,
  claudeModelOptions,
  claudeModelsLoading,
  codexModelOptions,
  codexModelsLoading,
  pickedEffort,
  pickedHarness,
  costControlMode,
  setPermissionMode,
  setApprovalMode,
  setCursorExecMode,
  setBypassSandbox,
  setPickedModel,
  setPickedEffort,
  setPickedHarness,
  setCostControlMode,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agent: AvailableAgent;
  brainHarnessLabels: Record<string, string>;
  host: Host | undefined | null;
  hideUnconfigured: boolean;
  smartRoutingEligible: boolean;
  permissionMode: string;
  approvalMode: string;
  cursorExecMode: string;
  bypassSandbox: boolean;
  pickedModel: string;
  claudeModelOptions: readonly Pick<NativeModelOption, "id" | "displayName" | "isDefault">[];
  claudeModelsLoading: boolean;
  codexModelOptions: readonly Pick<NativeModelOption, "id" | "displayName" | "isDefault">[];
  codexModelsLoading: boolean;
  pickedEffort: string;
  pickedHarness: string | null;
  costControlMode: CostControlMode;
  setPermissionMode: (mode: string) => void;
  setApprovalMode: (mode: string) => void;
  setCursorExecMode: (mode: string) => void;
  setBypassSandbox: (enabled: boolean) => void;
  setPickedModel: (model: string) => void;
  setPickedEffort: (effort: string) => void;
  setPickedHarness: (harness: string | null, agentId?: string) => void;
  setCostControlMode: (mode: CostControlMode) => void;
}) {
  const info = useServerInfo();
  // Feature ON → single "needs setup" badge; OFF → per-reason original text.
  const collapsedBadge = info !== "loading" && info.harness_install_enabled;
  const entryHarness = nativeCodingAgentForAvailableAgent(agent)?.harness ?? null;
  const hasPermission = nativeAgentHasCapability(agent, "permissionMode");
  const hasApproval = nativeAgentHasCapability(agent, "approvalMode");
  const hasCursor = nativeAgentHasCapability(agent, "cursorMode");
  const isCodex = entryHarness === "codex-native";
  const modelOptions = isCodex ? codexModelOptions : claudeModelOptions;
  const modelsLoading = isCodex ? codexModelsLoading : claudeModelsLoading;
  const brainDefault =
    agent.harness != null && agent.harness in brainHarnessLabels ? agent.harness : null;

  // Local draft — seeded from the live state each time the modal opens so
  // Cancel can discard and re-opening always reflects the committed state.
  const [draftModel, setDraftModel] = useState(pickedModel);
  const [draftEffort, setDraftEffort] = useState(pickedEffort);
  const [draftPermission, setDraftPermission] = useState(permissionMode);
  const [draftApproval, setDraftApproval] = useState(approvalMode);
  const [draftCursor, setDraftCursor] = useState(cursorExecMode);
  const [draftBypass, setDraftBypass] = useState(bypassSandbox);
  const [draftHarness, setDraftHarness] = useState<string | null>(pickedHarness);
  const [draftRouting, setDraftRouting] = useState<CostControlMode>(costControlMode);

  useEffect(() => {
    if (!open) return;
    setDraftModel(pickedModel);
    setDraftEffort(pickedEffort);
    setDraftPermission(permissionMode);
    setDraftApproval(approvalMode);
    setDraftCursor(cursorExecMode);
    setDraftBypass(bypassSandbox);
    setDraftHarness(pickedHarness);
    setDraftRouting(costControlMode);
    // Seed once per open from the current live values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Only treat routing as "on" when it's actually offered for this agent —
  // otherwise a stale costControlMode="on" (e.g. server later disabled the
  // flag) would select the __smart__ sentinel with no matching Select item.
  const smartRoutingOn = smartRoutingEligible && draftRouting === "on";
  // Fully-auto: the router picks the harness AND the model, so the only knob
  // left is what the picked harness may do without asking. Everything else is
  // harness-specific and can't be decided before the pick.
  // Both auto flavors land here: the brain-harness override on a bundle agent,
  // and top-level Smart Routing (which rides a placeholder native wrapper, so
  // it has no brainDefault of its own).
  const autoNative = draftHarness === AUTO_NATIVE_HARNESS_ID;
  const autoRouting = autoNative || (brainDefault != null && draftHarness === AUTO_HARNESS_ID);
  // Only top-level Smart Routing replaces the agent's identity (it rides a
  // placeholder wrapper, so naming that wrapper would misreport what runs). A
  // bundle agent's routed brain is a knob on that agent, so the modal keeps its
  // name: "Configure Debby", not "Configure Smart Routing".
  const configTitleName = autoNative ? SMART_ROUTING_LABEL : agent.display_name;
  const modelValue = smartRoutingOn ? MODEL_SELECT_SMART : draftModel || MODEL_SELECT_DEFAULT;
  const claudeModelSelectOptions = useMemo(
    () => claudeModelOptions.map((m) => ({ id: m.id, label: displayModelName(m) })),
    [claudeModelOptions],
  );
  const codexModelSelectOptions = useMemo(
    () => codexModelOptions.map((m) => ({ id: m.id, label: displayModelId(m) })),
    [codexModelOptions],
  );
  const onModelChange = (value: string) => {
    if (value === MODEL_SELECT_SMART) {
      setDraftRouting("on");
      setDraftModel("");
      // The router picks the model (and its effort) per turn, so an explicit
      // effort is meaningless — reset it so it doesn't ride along frozen.
      setDraftEffort("");
    } else if (value === MODEL_SELECT_DEFAULT) {
      setDraftModel("");
      // "Default" = no override; defer routing to the spec default (null,
      // omitted from create) — never emit an explicit "on"/"off".
      setDraftRouting(null);
    } else {
      setDraftModel(value);
      // Picking an explicit model turns routing off (mutually exclusive).
      setDraftRouting(null);
    }
  };

  const save = () => {
    // Top-level Smart Routing has nothing to commit — the router owns the
    // harness and model, and Permissions is locked to Default. Committing the
    // placeholder wrapper's model/mode drafts would leak its knobs into the
    // create call.
    if (autoNative) {
      onOpenChange(false);
      return;
    }
    if (hasPermission) {
      // Order matters: commit model first (its setter clears routing when a
      // model is set), then routing (its setter clears the model when "on") —
      // the two setters enforce the mutual exclusion between them.
      setPickedModel(draftModel);
      setPickedEffort(draftEffort);
      setPermissionMode(draftPermission);
      if (entryHarness)
        writeHarnessOption(entryHarness, {
          model: draftModel,
          effort: draftEffort,
          mode: draftPermission,
        });
    } else if (hasApproval) {
      if (isCodex) setPickedModel(draftModel);
      setApprovalMode(draftApproval);
      setBypassSandbox(draftBypass);
      if (entryHarness)
        writeHarnessOption(entryHarness, {
          mode: draftApproval,
          ...(isCodex ? { model: draftModel } : {}),
        });
    } else if (hasCursor) {
      setCursorExecMode(draftCursor);
      if (entryHarness) writeHarnessOption(entryHarness, { mode: draftCursor });
    } else if (brainDefault) {
      // Picking the spec default clears the override so the session tracks it.
      setPickedHarness(draftHarness === brainDefault ? null : draftHarness, agent.id);
    }
    // Smart Routing rides the Model dropdown on both routable harnesses
    // (Claude Code and Codex), so commit it outside the per-capability branches.
    // Remembered per harness like the model pick, so the next new session with
    // this harness starts on it again.
    if (smartRoutingEligible) {
      setCostControlMode(draftRouting);
      if (draftRouting === "on") {
        // Routing owns the model and its effort, so clear both — live state AND
        // the harness's remembered pick. A harness whose modal has no model
        // picker (Codex) never touches them in its own branch above, so a model
        // remembered from an earlier build would otherwise survive and ride
        // along with routing, which the server reads as an already-pinned model.
        setPickedModel("");
        setPickedEffort("");
      }
      if (entryHarness)
        writeHarnessOption(entryHarness, {
          routing: draftRouting === "on" ? "on" : "off",
          ...(draftRouting === "on" ? { model: "", effort: "" } : {}),
        });
    }
    onOpenChange(false);
  };

  const brainEntries = brainDefault
    ? Object.entries(brainHarnessLabels).filter(
        ([id]) =>
          id === (draftHarness ?? brainDefault) ||
          !hideUnconfigured ||
          !harnessUnconfiguredOnHost(id, host),
      )
    : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="new-chat-landing-config-modal">
        <DialogHeader>
          <DialogTitle>Configure {configTitleName}</DialogTitle>
          <DialogDescription className="sr-only">
            Configure how {configTitleName} runs for this session.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5 py-1">
          {!autoRouting && hasPermission && (
            <>
              <ConfigRow label="Model" description="Underlying LLM">
                <RoutingModelSelect
                  value={modelValue}
                  onValueChange={onModelChange}
                  offerSmartRouting={smartRoutingEligible}
                  testId="new-chat-landing-config-model"
                  models={claudeModelSelectOptions}
                  contentClassName="[&_[data-slot=select-item]]:pl-2.5"
                >
                  {claudeModelsLoading && (
                    <div className="px-2.5 py-1 text-sm text-muted-foreground">Loading models…</div>
                  )}
                  {!claudeModelsLoading && claudeModelOptions.length === 0 && (
                    <div className="px-2.5 py-1 text-sm text-muted-foreground">
                      Models unavailable
                    </div>
                  )}
                </RoutingModelSelect>
              </ConfigRow>

              <ConfigRow label="Effort" description="Reasoning depth vs. speed">
                <Select
                  // Smart Routing picks the model (and its effort) per
                  // turn, so an explicit effort is meaningless: the row is
                  // frozen and reads as an em-dash placeholder. Radix shows the
                  // placeholder for the empty value, which no item can carry.
                  value={smartRoutingOn ? "" : draftEffort || EFFORT_SELECT_NONE}
                  onValueChange={(v) => setDraftEffort(v === EFFORT_SELECT_NONE ? "" : v)}
                  disabled={smartRoutingOn}
                >
                  <SelectTrigger
                    className="w-full cursor-pointer"
                    data-testid="new-chat-landing-config-effort"
                    aria-label="Reasoning effort"
                  >
                    <SelectValue placeholder={EFFORT_UNAVAILABLE_PLACEHOLDER} />
                  </SelectTrigger>
                  <SelectContent
                    position="popper"
                    align="start"
                    className="w-(--radix-select-trigger-width) [&_[data-slot=select-item]]:pl-2.5"
                  >
                    <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
                    {CLAUDE_NATIVE_EFFORTS.map((e) => (
                      <SelectItem key={e.value} value={e.value}>
                        {e.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </ConfigRow>

              <ConfigRow label="Permissions" description="What the agent can do without asking">
                <DescribedSelect
                  value={draftPermission}
                  onValueChange={setDraftPermission}
                  options={CLAUDE_NATIVE_PERMISSION_MODES}
                  testId="new-chat-landing-config-permission"
                  ariaLabel="Permissions"
                />
              </ConfigRow>
            </>
          )}

          {!autoRouting && hasApproval && (
            <>
              {/* Codex's catalog is resolved by the host, so the row lists real
              models alongside the two choices the create call can express on its
              own: the harness's default, or the router picking per turn (only
              when routing is offered). */}
              <ConfigRow label="Model" description="Underlying LLM">
                <RoutingModelSelect
                  value={modelValue}
                  onValueChange={onModelChange}
                  offerSmartRouting={smartRoutingEligible}
                  testId="new-chat-landing-config-model"
                  models={codexModelSelectOptions}
                  defaultLabel={defaultModelLabel(codexModelOptions, displayModelId)}
                  contentClassName="[&_[data-slot=select-item]]:pl-2.5"
                >
                  {modelsLoading && (
                    <div className="px-2.5 py-1 text-sm text-muted-foreground">Loading models…</div>
                  )}
                  {!modelsLoading && modelOptions.length === 0 && (
                    <div className="px-2.5 py-1 text-sm text-muted-foreground">
                      Models unavailable
                    </div>
                  )}
                </RoutingModelSelect>
              </ConfigRow>
              <ConfigRow label="Approval" description="What the agent can do without asking">
                <DescribedSelect
                  // Codex adds the DANGEROUS full-bypass as a 4th option; when
                  // armed the select shows it (draftBypass wins over the preset).
                  value={
                    isCodex && draftBypass ? CODEX_NATIVE_BYPASS_APPROVAL_VALUE : draftApproval
                  }
                  onValueChange={(v) => {
                    if (v === CODEX_NATIVE_BYPASS_APPROVAL_VALUE) {
                      setDraftBypass(true);
                    } else {
                      setDraftBypass(false);
                      setDraftApproval(v);
                    }
                  }}
                  options={
                    isCodex
                      ? [...CODEX_NATIVE_APPROVAL_MODES, CODEX_NATIVE_BYPASS_APPROVAL_OPTION]
                      : CODEX_NATIVE_APPROVAL_MODES
                  }
                  testId="new-chat-landing-config-approval"
                  ariaLabel="Approval"
                />
              </ConfigRow>
            </>
          )}

          {!autoRouting && hasCursor && (
            <ConfigRow label="Mode" description="How Cursor runs commands">
              <DescribedSelect
                value={draftCursor}
                onValueChange={setDraftCursor}
                options={CURSOR_NATIVE_EXEC_MODES}
                testId="new-chat-landing-config-cursor-mode"
                ariaLabel="Mode"
              />
            </ConfigRow>
          )}

          {/* Stays rendered while Smart Routing is the pick: it is the control
          that selected it, so hiding it would strand the choice with no way to
          read it back or switch away without cancelling. */}
          {!hasPermission && !hasApproval && !hasCursor && brainDefault && (
            <ConfigRow label="Agent Harness" description="Underlying coding harness">
              <Select value={draftHarness ?? brainDefault} onValueChange={setDraftHarness}>
                <SelectTrigger
                  className="w-full cursor-pointer"
                  data-testid="new-chat-landing-config-harness"
                  aria-label="Agent Harness"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent
                  position="popper"
                  align="start"
                  className="[&_[data-slot=select-item]]:pl-2.5"
                >
                  {brainEntries.map(([id, label]) => (
                    <SelectItem key={id} value={id} data-testid={`new-chat-landing-harness-${id}`}>
                      <span className="flex items-center gap-2">
                        {label}
                        {/* Only the auto row carries a blurb: "Auto" alone
                        doesn't say what gets picked. Same muted style the agent
                        picker uses for its row descriptions. */}
                        {id === AUTO_HARNESS_ID && (
                          <span className="truncate text-[11px] text-muted-foreground/70">
                            {AUTO_HARNESS_DESCRIPTION}
                          </span>
                        )}
                        {harnessUnconfiguredOnHost(id, host) && (
                          <Badge
                            variant="outline"
                            className="border-amber-300 bg-amber-50 text-sm text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
                            data-testid={`new-chat-landing-harness-warning-${id}`}
                          >
                            {harnessWarningBadgeText(
                              harnessUnavailableReasonOnHost(id, host),
                              collapsedBadge,
                            )}
                          </Badge>
                        )}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </ConfigRow>
          )}

          {/* Top-level Smart Routing: the router owns the model, so Permissions
          is the last decidable row — and it is locked to Default until a
          cross-harness permission mapping exists. Default means "send no
          override", so the picked harness inherits the machine's own Claude Code
          / Codex config. A bundle agent's routed brain gets no such row: its
          create call (claude-sdk) never carries a permission field, so the row
          would only be decoration on top of the Agent Harness pick. */}
          {autoNative && (
            <ConfigRow label="Permissions" description="What the agent can do without asking">
              <DescribedSelect
                value={CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE}
                onValueChange={() => {}}
                options={AUTO_PERMISSION_MODE_OPTIONS}
                testId="new-chat-landing-config-permission"
                ariaLabel="Permissions"
                disabled
              />
            </ConfigRow>
          )}
        </div>

        <DialogFooter className="border-t-0 bg-transparent">
          <Button
            type="button"
            size="lg"
            variant="outline"
            onClick={() => onOpenChange(false)}
            data-testid="new-chat-landing-config-cancel"
          >
            Cancel
          </Button>
          <Button type="button" onClick={save} data-testid="new-chat-landing-config-save" size="lg">
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// In-memory draft for the new-session landing screen, so a half-composed
// message, attachments and picker selections survive the unmount that happens
// when the user navigates into an existing session and back. Module-scoped,
// not persisted to storage (a page refresh starts clean); cleared on create.
interface LandingDraft {
  message: string;
  files: File[];
  pickedAgentId: string | null;
  selectedHostId: string | null;
  sandboxSelected: boolean;
  sandboxRepoUrl: string;
  sandboxRepoBranch: string;
  workspace: string;
  branchName: string;
  prefilledBranch: string;
  permissionMode: string;
  approvalMode: string;
  bypassSandbox: boolean;
  cursorExecMode: string;
  pickedHarness: string | null;
  pickedModel: string;
  pickedEffort: string;
  costControlMode: CostControlMode;
}

let landingDraft: LandingDraft | null = null;

// Test-only: clears the preserved landing draft so each case starts from a
// clean module state (the draft is module-scoped and survives unmount by
// design, which would otherwise leak between tests).
export function resetLandingDraft(): void {
  landingDraft = null;
}

export function NewChatLandingScreen() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const serverUrl = getCliServerUrl();
  const { data: agents } = useAvailableAgents();
  // refetchOnFocus: returning from a terminal `omni setup` must clear the
  // readiness badge even if the live push was missed while the tab was hidden.
  const { data: hosts, isLoading: hostsLoading } = useHosts({ refetchOnFocus: true });

  const agentList = useMemo(
    () =>
      sortAgentsForDisplay((agents ?? []).filter((a) => !NEW_SESSION_HIDDEN_AGENTS.has(a.name))),
    [agents],
  );

  // Split the picker into "Harnesses" (the native terminal CLIs) and
  // "Agents" (SDK / bundle agents like Polly & Debby plus any custom
  // user-registered agents). This is the isNativeCodingAgent split, NOT the
  // builtins/customs split: Polly & Debby are built-ins but belong under
  // "Agents", not "Harnesses".
  const harnessEntries = useMemo(
    () => agentList.filter((a) => isNativeCodingAgent(a)),
    [agentList],
  );
  const agentEntries = useMemo(() => agentList.filter((a) => !isNativeCodingAgent(a)), [agentList]);

  // "Create custom agent" dialog state and pending bundle. When the user
  // creates a custom agent via the dialog, the bundle input is stored
  // here and the picker switches to a virtual "pending" agent entry. On
  // form submit, handleCreate detects the pending bundle, builds the
  // tar.gz, and uses multipart POST instead of the normal JSON path.
  const [createAgentOpen, setCreateAgentOpen] = useState(false);
  const [pendingAgent, setPendingAgent] = useState<AgentBundleInput | null>(null);
  // Sentinel id for the pending custom agent in the picker dropdown.
  const PENDING_AGENT_ID = "__pending_custom_agent__";

  // Surface element backing the iOS native server switcher overlay, which
  // the in-session view shows too — the picker stays reachable while starting
  // a new session. The hook hides it whenever the sidebar covers the surface.
  const [landingSurface, setLandingSurface] = useState<HTMLElement | null>(null);
  useNativeServerSwitcherForMainSurface(landingSurface, true);

  const [message, setMessage] = useState<string>(() => landingDraft?.message ?? "");
  // Composer text captured when voice dictation starts, so Esc can revert to it.
  const voiceSnapshotRef = useRef("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Declared after textareaRef so dictation can place the caret after the
  // text it inserts (and insert at the caret rather than the draft's end).
  const dictation = useDictationInsert(message, setMessage, textareaRef);
  const isComposingRef = useRef(false);
  // maxRows 9 = 180px of 20px lines, matching the composer's 200px
  // border-box max (180px content + 16px top / 4px bottom padding).
  useAutoGrowTextarea(textareaRef, message, 9);

  // Attachments for the first message — same affordances as the in-session
  // composer (paperclip + paste); carried to ChatPage via the pending
  // initial prompt and sent with the auto-dispatched first turn.
  const [files, setFiles] = useState<File[]>(() => landingDraft?.files ?? []);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const addFiles = (incoming: File[]) => setFiles((prev) => [...prev, ...incoming]);
  const removeFile = (index: number) => setFiles((prev) => prev.filter((_, i) => i !== index));

  // Drag-and-drop onto the composer — same behavior as the in-session
  // composer (drop files anywhere on the box; an inset ring + overlay
  // signal the drop target).
  const [isDragActive, setIsDragActive] = useState(false);

  const handleDrop = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(false);
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length > 0) addFiles(dropped);
  };

  const handleDragOver = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragEnter = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Only clear the active state when the pointer leaves the container
    // itself, not when it moves between child elements inside it.
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDragActive(false);
  };

  // Gates the sandbox host option: only servers whose sandbox
  // config can actually serve a managed launch advertise it. "loading"
  // fails closed (option hidden) until the boot probe resolves.
  const info = useServerInfo();
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  const smartRoutingEnabled = info !== "loading" && info.smart_routing_enabled;
  // Which router can answer a pick. The external AI-Gateway router only covers
  // a family the host runs through the gateway; the built-in judge covers any
  // family. Read once here and reused by every routing gate below. "loading"
  // reads as neither, so no routing surface flashes in before the probe lands.
  const externalRoutingConfigured = info !== "loading" && info.smart_routing_sources.external;
  const ossRoutingConfigured = info !== "loading" && info.smart_routing_sources.oss;
  // Gates the whole UI-driven setup experience (Set up affordance + dialog +
  // collapsed badge). OFF → the composer/picker fall back to the original
  // "run omni setup" guidance, so a disabled flag is a no-op on the UI.
  const harnessInstallEnabled = info !== "loading" && info.harness_install_enabled;
  // Unfiltered brain-harness labels: safe for membership checks and for
  // labelling an existing pick, but the OPTIONS offered in the gear modal use
  // the gated `brainHarnessLabels` below, which drops the fully-auto row when
  // neither router can back both arms.
  const brainHarnessLabelsAll = useBrainHarnessLabels(smartRoutingEnabled);
  // Provider-named label for the sandbox option (e.g. "Modal Sandbox"),
  // falling back to the generic "New Sandbox" when the server names no
  // provider.
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  // Embed-only docs seam: when the host passes additional docs and managed
  // sandboxes are unavailable, keep the sandbox row visible but disabled and
  // attach a help tooltip with a clickable link.
  const docsLinks = getOmnigentHostConfig().docsLinks;
  const newSandboxTooltipContent = docsLinks?.newSandbox;
  // Embed-only docs seam for Databricks git auth setup. Standalone leaves this
  // undefined, so no tooltip is rendered.
  const databricksGitCredentialsTooltipContent = docsLinks?.databricksGitCredentials;
  const showDisabledSandboxWithDocs = !managedSandboxesEnabled && !!newSandboxTooltipContent;

  // Project driving this visit, when the sidebar's per-project "new session"
  // pencil landed here with a `?project=` query param. Empty otherwise.
  const projectParam = searchParams.get("project") ?? "";
  // Seeded from the persisted last pick so a returning user starts on the
  // agent they used last; validated against the live list in
  // effectiveAgentId below (a stale id falls back to the default). A
  // project-driven visit defers to the project-prefill effect instead
  // (which falls back to the same last pick).
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(
    () => landingDraft?.pickedAgentId ?? (projectParam !== "" ? null : readLastAgentId()),
  );
  const [selectedHostId, setSelectedHostId] = useState<string | null>(
    () => landingDraft?.selectedHostId ?? null,
  );
  // Sessions on the selected host — fetched only when a host is selected,
  // to avoid registering hundreds of sessions into the health poll at idle.
  const { data: directorySessions } = useDirectorySessions(selectedHostId !== null);
  // True when the user picked the sandbox option instead of a connected
  // host — the server provisions a sandbox host at create time
  // (host_type: "managed"), so no host_id or workspace is sent.
  const [sandboxSelected, setSandboxSelected] = useState(
    () => landingDraft?.sandboxSelected ?? false,
  );
  const { data: hostClaudeModelOptions, isLoading: hostClaudeModelsLoading } = useHostModelOptions(
    selectedHostId,
    "claude-native",
    !sandboxSelected,
  );
  const { data: hostCodexModelOptions, isLoading: hostCodexModelsLoading } = useHostModelOptions(
    selectedHostId,
    "codex-native",
    !sandboxSelected,
  );
  const claudeModelOptions = useMemo(
    () =>
      sandboxSelected
        ? CLAUDE_NATIVE_MODELS.map((model) => ({
            id: model.id,
            displayName: model.label,
          }))
        : (hostClaudeModelOptions ?? []).map((option) => ({
            id: option.id,
            displayName: option.displayName ?? option.id,
          })),
    [hostClaudeModelOptions, sandboxSelected],
  );
  const codexModelOptions = useMemo(
    () => (sandboxSelected ? [] : (hostCodexModelOptions ?? [])),
    [hostCodexModelOptions, sandboxSelected],
  );
  // Desktop-shell host status for THIS machine (null outside Electron), so the
  // picker can tag the current machine and offer to auto-connect it.
  const [desktopHost, setDesktopHost] = useState<HostIdentity | null>(null);
  const [connectingThisMachine, setConnectingThisMachine] = useState(false);
  // Defer the connect until the dropdown has actually closed (set on select,
  // consumed in the menu's onOpenChange) — connecting while the menu is open
  // looks janky. A ref so the close handler sees it synchronously.
  const pendingConnectRef = useRef(false);
  // Sandbox repository inputs — composed into the managed create's
  // `workspace` string (`<url>[#<branch>]`); both blank = empty
  // server-created workspace.
  const [sandboxRepoUrl, setSandboxRepoUrl] = useState<string>(
    () => landingDraft?.sandboxRepoUrl ?? "",
  );
  const [sandboxRepoBranch, setSandboxRepoBranch] = useState<string>(
    () => landingDraft?.sandboxRepoBranch ?? "",
  );
  const [workspace, setWorkspace] = useState<string>(() => landingDraft?.workspace ?? "");
  const [branchName, setBranchName] = useState<string>(() => landingDraft?.branchName ?? "");
  // The base branch auto-fills from the configured default (Settings › Git)
  // when the user names a worktree branch, and is left alone once the user
  // touches it — clearing the branch name re-arms the auto-fill (see the effect
  // below). `baseBranchEdited` tracks that hand-off; any edit (including
  // clearing the field) sets it so a later re-seed won't clobber the choice.
  const [baseBranch, _setBaseBranch] = useState<string>("");
  const [baseBranchEdited, setBaseBranchEdited] = useState<boolean>(false);
  const setBaseBranch = useCallback((next: string) => {
    _setBaseBranch(next);
    setBaseBranchEdited(true);
  }, []);
  // Branch prefilled from the existing worktree the current workspace points
  // at. When `branchName` still equals this, the session starts directly in
  // that worktree (no git opts). Editing the field away from it means the user
  // wants a *new* worktree off that name.
  const [prefilledBranch, setPrefilledBranch] = useState<string>(
    () => landingDraft?.prefilledBranch ?? "",
  );
  // Project to file the new session under. Empty = unfiled. Stamped as the
  // `omni_project` label at create (so the row is filed from its first sidebar
  // appearance), then promoted to first-class `project_id` right after.
  // Pre-filled from the `?project=` param so the sidebar's per-project
  // "new session" pencil lands here with the project already selected.
  const [selectedProject, setSelectedProject] = useState<string>(() => projectParam);
  // The landing screen stays mounted while the `?project=` param changes (e.g.
  // clicking a different project's pencil), so the lazy initializer above won't
  // re-run — sync the selection to the param whenever it changes.
  useEffect(() => {
    setSelectedProject(projectParam);
  }, [projectParam]);
  // Permission mode for Claude Code (claude --permission-mode). Only
  // meaningful for the claude-native wrapper; ignored otherwise. Lives in
  // the footer tray's Advanced settings menu.
  const [permissionMode, setPermissionMode] = useState<string>(
    () => landingDraft?.permissionMode ?? CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  );
  // Approval mode for Codex (codex --approval-mode). Only meaningful for
  // the codex-native wrapper; ignored otherwise. Lives in the footer
  // tray's Advanced settings menu.
  const [approvalMode, setApprovalMode] = useState<string>(
    () => landingDraft?.approvalMode ?? CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
  );
  // DANGEROUS codex full-bypass opt-in (Codex only). OFF by default and only
  // flippable on after the user types the confirmation phrase, so it can
  // never be enabled by an accidental click. Persisted as a conversation
  // label so it survives reload. When on, a persistent red banner warns and
  // the runner ignores the approval-mode preset's flags.
  const [bypassSandbox, setBypassSandbox] = useState<boolean>(
    () => landingDraft?.bypassSandbox ?? false,
  );
  // Execution mode for Cursor (cursor-agent --mode / --yolo). Only meaningful
  // for the cursor-native wrapper; ignored otherwise.
  const [cursorExecMode, setCursorExecMode] = useState<string>(
    () => landingDraft?.cursorExecMode ?? CURSOR_NATIVE_DEFAULT_EXEC_MODE,
  );
  // Per-session brain-harness override for bundle agents (polly / debby).
  // null = the agent spec's declared harness (no override sent). On agent
  // switch, seeded from the user's last stored pick for that agent.
  const [pickedHarness, setPickedHarness] = useState<string | null>(
    () =>
      landingDraft?.pickedHarness ??
      readLastHarness(landingDraft?.pickedAgentId ?? readLastAgentId()),
  );
  // Per-session model + reasoning effort for the claude-native model picker.
  // "" = unselected: nothing is checked and `model_override` / `reasoning_effort`
  // are omitted from the create, so Claude Code uses its own configured model.
  // An explicit pick rides along and is remembered (seeded back on a later visit
  // via the harness-seed effect below).
  const [pickedModel, _setPickedModel] = useState<string>(() => landingDraft?.pickedModel ?? "");
  const [pickedEffort, setPickedEffort] = useState<string>(() => landingDraft?.pickedEffort ?? "");
  // Per-session cost-control switch ("Cost Optimized" pill). Unset
  // (null) defers to the agent spec's default and is omitted from
  // the create body.
  const [costControlMode, _setCostControlMode] = useState<CostControlMode>(
    () => landingDraft?.costControlMode ?? null,
  );
  // Model selection and smart routing are mutually exclusive: enabling
  // routing clears the explicit model pick, and picking a model turns
  // routing off.
  const setPickedModel = useCallback((model: string) => {
    _setPickedModel(model);
    if (model) _setCostControlMode(null);
  }, []);
  const setCostControlMode = useCallback((mode: CostControlMode) => {
    _setCostControlMode(mode);
    if (mode === "on") _setPickedModel("");
  }, []);
  // Controls the working-directory popover so picking a directory closes it.
  const [workspacePopoverOpen, setWorkspacePopoverOpen] = useState(false);
  // Controlled so selecting an existing worktree can close the popover.
  const [worktreePopoverOpen, setWorktreePopoverOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // "Connect a host" instructions modal, opened from the host dropdown.
  const [connectOpen, setConnectOpen] = useState(false);
  // Harness "Set up" dialog target, opened from the composer notice or a picker
  // row; null when closed. One dialog serves every entry point.
  const [setupTarget, setSetupTarget] = useState<{
    agentName: string | undefined;
    harness: string | null;
    host: Host | undefined | null;
  } | null>(null);
  // Harness-config modal, opened from the composer's gear icon.
  const [configOpen, setConfigOpen] = useState(false);

  // Mirror the current draft fields into a ref every render so the unmount
  // cleanup below can snapshot the latest values without re-subscribing.
  // `submittedRef` is flipped once the draft is sent to a create, so the
  // snapshot is dropped instead of resurrected.
  const submittedRef = useRef(false);
  // Whether this composer is still on screen. The create POST can outlive
  // it — the user opens another session while the session bootstraps — and
  // the post-create navigation must not follow them there.
  const onScreenRef = useRef(true);
  const draftRef = useRef<LandingDraft>(null as unknown as LandingDraft);
  draftRef.current = {
    message,
    files,
    pickedAgentId,
    selectedHostId,
    sandboxSelected,
    sandboxRepoUrl,
    sandboxRepoBranch,
    workspace,
    branchName,
    prefilledBranch,
    permissionMode,
    approvalMode,
    bypassSandbox,
    cursorExecMode,
    pickedHarness,
    pickedModel,
    pickedEffort,
    costControlMode,
  };
  useEffect(() => {
    // Re-set on setup so StrictMode's setup→cleanup→setup double-invoke
    // doesn't leave the screen marked gone.
    onScreenRef.current = true;
    return () => {
      onScreenRef.current = false;
      landingDraft = submittedRef.current ? null : draftRef.current;
    };
  }, []);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);
  const { addRecentHarness } = useRecentHarnesses();

  const allHosts = hosts ?? [];
  const onlineHosts = allHosts.filter((h) => h.status === "online");
  const offlineHosts = allHosts.filter((h) => h.status === "offline");

  // Identify this machine exactly through Electron. A standalone browser cannot
  // read local host config, so a loopback server with exactly one online host
  // uses that host as a conservative local-development fallback.
  const thisMachineHostId = resolveThisMachineHostId(
    desktopHost?.hostId ?? null,
    isCurrentServerLocal(),
    onlineHosts.map((host) => host.host_id),
  );
  // When it's already in the host list (online or offline) we connect via that
  // row; only when it's absent do we show a standalone "Run on this machine"
  // item, so the machine never appears twice.
  const thisMachineInList =
    thisMachineHostId != null && allHosts.some((h) => h.host_id === thisMachineHostId);
  const canConnectThisMachine = Boolean(desktopHost?.cliInstalled);
  const showConnectThisMachine = canConnectThisMachine && !thisMachineInList;

  // Track this machine's host status from the desktop shell (no-op in a browser).
  useEffect(() => {
    if (!isElectronShell()) return;
    let cancelled = false;
    const refresh = () => {
      void getHostIdentity().then((s) => {
        if (!cancelled) setDesktopHost(s);
      });
    };
    refresh();
    const unsubscribe = onHostStatusChanged(refresh);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  // Project prefill: a project-driven visit seeds the composer from the
  // project's stored defaults (host / working directory / agent / worktree).
  // `?project=` carries the project NAME, so resolve it to the first-class id
  // the config endpoint needs; a label-only folder (id null) or plain visit
  // has no config to read.
  const { data: projectList, isLoading: projectListLoading } = useProjects();
  const configProjectId = useMemo(
    () =>
      projectParam !== ""
        ? ((projectList ?? []).find((p) => p.name === projectParam)?.id ?? null)
        : null,
    [projectList, projectParam],
  );
  const { data: storedProjectConfig, isLoading: projectConfigLoading } =
    useProjectConfig(configProjectId);
  // Normalize into the machine's shape. `undefined` = still loading (the machine
  // waits so a generic default can't win the race); `{}` = nothing to wait for
  // (plain visit / label-only folder / genuinely empty config), so it settles
  // immediately and the generic defaults take over.
  const prefillConfig = useMemo<ProjectPrefillConfig | undefined>(() => {
    // A project-scoped visit must resolve name → id via the projects list
    // before we know whether there's a config to read — until it loads, the id
    // is falsely null, so wait rather than settle prematurely.
    if (projectParam !== "" && projectListLoading) return undefined;
    if (configProjectId !== null && projectConfigLoading) return undefined;
    const c = storedProjectConfig;
    if (!c) return {};
    return {
      hostId: c.host_id,
      workspace: c.workspace,
      agentId: c.agent_id,
      useWorktree: c.use_worktree,
    };
  }, [
    projectParam,
    projectListLoading,
    configProjectId,
    projectConfigLoading,
    storedProjectConfig,
  ]);
  // State machine driving the project prefill: a location seed (host +
  // workspace from config) plus an independent agent seed. The generic
  // host/workspace defaults below hold off until it settles so they can't win
  // the race against the project's stored values.
  const [prefill, setPrefill] = useState<ProjectPrefillState>(() =>
    initialPrefillState(projectParam),
  );
  // The generic defaults gate on the location track only — the agent seed
  // waits on its own fetch and must not hold up the host/workspace fill.
  const prefillSettled = prefill.phase === "settled";
  // Host whose workspace was already seeded once, so a host re-pick doesn't
  // clobber the field (used by the per-host seeding effect below).
  const seededHostRef = useRef<string | null>(null);
  // Workspace the opt-in worktree effect already acted on, so it fires at most
  // once per settled workspace (and can't loop once it sets a branch name).
  const worktreeSeededForRef = useRef<string | null>(null);

  // Signature of the stored config the machine last settled from. Lets a later
  // save be noticed even when the pencil re-opens the SAME project — the config
  // content changes while `projectParam` does not. `null` = not yet seeded.
  const seededConfigSigRef = useRef<string | null>(null);
  const prefillConfigSig = useMemo(
    () => (prefillConfig === undefined ? null : JSON.stringify(prefillConfig)),
    [prefillConfig],
  );

  // The landing screen stays mounted while `?project=` changes (clicking
  // another project's pencil), so re-create a fresh visit by hand: clear
  // every seedable slot and restart the machine. Values the user set are
  // reset too — a pencil click means "set me up for this project". Also
  // restart when the SAME project's stored defaults change (the user edited
  // its settings, then re-opened its composer): `projectParam` stays put, so
  // without this the already-settled machine would keep the stale seeds.
  useEffect(() => {
    const projectChanged = prefill.project !== projectParam;
    const configChanged =
      !projectChanged &&
      projectParam !== "" &&
      prefillConfigSig !== null &&
      seededConfigSigRef.current !== null &&
      prefillConfigSig !== seededConfigSigRef.current;
    if (!projectChanged && !configChanged) return;
    setSandboxSelected(false);
    setSelectedHostId(null);
    setPickedAgentId(projectParam !== "" ? null : readLastAgentId());
    setWorkspace("");
    setBranchName("");
    seededHostRef.current = null;
    worktreeSeededForRef.current = null;
    seededConfigSigRef.current = prefillConfigSig;
    setPrefill(initialPrefillState(projectParam));
  }, [projectParam, prefill.project, prefillConfigSig]);

  // Record the config the machine settled from, once it's loaded and the
  // machine is done, so the reseed effect above can spot a later change to it
  // (the reseed on a project switch runs before the config has loaded, leaving
  // the signature `null` until this fills it in).
  useEffect(() => {
    if (prefill.project !== projectParam) return;
    if (prefillConfigSig === null || !prefillDone(prefill)) return;
    seededConfigSigRef.current = prefillConfigSig;
  }, [prefill, projectParam, prefillConfigSig]);

  // Auto-select an option so a session can be started without an explicit
  // pick. Prefer the user's last explicit choice (persisted across visits);
  // otherwise fall back to the FIRST AVAILABLE option in menu order — the
  // sandbox when the server supports it (it's pinned first in the picker),
  // else the first online host. Only fills an empty slot; an explicit choice
  // already in state (or restored from the in-memory draft) is never
  // overridden. Holds off while a project prefill is deciding.
  useEffect(() => {
    if (!prefillSettled) return;
    if (sandboxSelected) return;
    if (selectedHostId !== null) return;

    // Read the persisted pick once, as a mount-time seed — deliberately NOT a
    // dependency: it only matters until the slot is filled, and re-running on
    // its value would fight an explicit in-session selection.
    const lastChoice = readLastHostChoice();
    if (lastChoice === SANDBOX_HOST_CHOICE) {
      // Wait for the server-info probe before acting on a sandbox pick: until
      // it resolves we don't know whether the sandbox is offered, and falling
      // through to a connected host would strand the returning sandbox user
      // (this effect wouldn't re-run to correct it once a host is set).
      if (info === "loading") return;
      if (managedSandboxesEnabled) {
        setSandboxSelected(true);
        return;
      }
      // Sandbox no longer offered (e.g. an OSS server) — fall through.
    } else if (lastChoice) {
      // A persisted host pick can only be honored once the host list has
      // loaded and shows it online. Wait for the load rather than defaulting
      // past it — defaulting to the sandbox here would set sandboxSelected and
      // this effect would then never re-run to restore the host.
      if (hostsLoading) return;
      const stored = (hosts ?? []).find((h) => h.host_id === lastChoice && h.status === "online");
      if (stored) {
        setSelectedHostId(stored.host_id);
        return;
      }
      // Stored host is gone or offline — fall through to the default.
    }

    if (managedSandboxesEnabled) {
      setSandboxSelected(true);
      return;
    }
    const firstOnline = (hosts ?? []).find((h) => h.status === "online");
    if (firstOnline) setSelectedHostId(firstOnline.host_id);
  }, [
    hosts,
    hostsLoading,
    selectedHostId,
    sandboxSelected,
    managedSandboxesEnabled,
    info,
    prefillSettled,
  ]);

  // Fall back to the host's home directory when it has no recorded recents, so
  // the working-directory field is pre-filled and the user can send in one
  // click. Derived from the same home listing the picker uses (entries carry
  // absolute paths); only fetched when there's no recent to fall back to.
  const needsHomeFallback = selectedHostId !== null && recent.length === 0;
  const { data: homeListing, isPlaceholderData: homeListingIsPlaceholder } = useHostFilesystem(
    selectedHostId,
    needsHomeFallback ? "" : null,
  );
  // The hook serves the PREVIOUS query's data as a placeholder while a new
  // fetch is in flight (an anti-flicker nicety for the picker), so right
  // after a host switch the listing briefly belongs to the old host.
  // Deriving home from it would seed the old host's path and lock the
  // once-per-host guard below — treat placeholder data as not-yet-loaded.
  const derivedHome = useMemo(
    () => (homeListingIsPlaceholder ? null : deriveHomeDir(homeListing?.entries ?? [])),
    [homeListing, homeListingIsPlaceholder],
  );

  // Fill the branch field with a unique auto-generated name so the user can
  // spin up a throwaway worktree without inventing one. crypto.randomUUID is
  // available in every browser the app targets; the short prefix keeps the
  // dir/branch readable (worktree-1a2b3c4d).
  const generateBranchName = useCallback(() => {
    const suffix = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
    setBranchName(`worktree-${suffix}`);
  }, []);
  // The project's stored default base branch (Project settings), trimmed. Wins
  // over the user-global default (Settings › Git); an unset project default
  // falls through to the global one, then to blank (fork from current branch).
  const projectBaseBranch = storedProjectConfig?.base_branch?.trim() || null;

  // The path the once-per-host auto-seed WOULD land on: the most-recent path,
  // else the derived home. Exposed as a memo so we can probe its repo for
  // worktrees before committing to it (see the fork-fresh redirect below).
  const autoSeedCandidate = useMemo(() => recent[0] ?? derivedHome ?? null, [recent, derivedHome]);
  // "Fork fresh from default": when the project defines a default base branch,
  // a fresh new-chat must NOT silently continue in the last-used worktree — it
  // should fork a new branch off that default. The auto-seed can land on a
  // linked worktree (a recent path that happens to be one), which prefills its
  // branch and suppresses the base-branch fill. So when a default is set, probe
  // the seed candidate's repo and redirect the seed to the MAIN work tree. Only
  // armed until the host is seeded (the once-per-host guard), and skipped for
  // sandboxes (no host worktrees).
  const forkFreshArmed =
    prefillSettled &&
    selectedHostId !== null &&
    !sandboxSelected &&
    projectBaseBranch !== null &&
    seededHostRef.current !== selectedHostId &&
    autoSeedCandidate !== null;
  const {
    data: seedWorktrees,
    isPlaceholderData: seedWorktreesArePlaceholder,
    isError: seedWorktreesErrored,
  } = useHostWorktrees(
    forkFreshArmed ? selectedHostId : null,
    forkFreshArmed ? autoSeedCandidate : null,
  );
  // Resolve the fork-fresh redirect to a STABLE value so the seed effect can
  // depend on the decision, not the worktree array (whose identity churns every
  // render). `undefined` = probe still loading (wait); `null` = not armed or no
  // redirect (seed the candidate as-is); a string = the MAIN repo path to
  // redirect the seed to (the candidate is a linked worktree we should fork off
  // the project default instead of reusing).
  const forkFreshMainPath = useMemo<string | null | undefined>(() => {
    if (!forkFreshArmed) return null;
    // A probe error (non-400; the hook already maps 400 → []) leaves data
    // undefined for good. Treat it as "no redirect" so the seed still lands on
    // the candidate as-is, rather than waiting on data that never arrives and
    // leaving the workspace blank forever.
    if (seedWorktreesErrored) return null;
    if (seedWorktreesArePlaceholder || seedWorktrees === undefined) return undefined;
    const norm = normalizeWorkspacePath(autoSeedCandidate);
    const candIsLinkedWorktree = seedWorktrees.some(
      (w) => !w.is_main && normalizeWorkspacePath(w.path) === norm,
    );
    const mainPath = seedWorktrees.find((w) => w.is_main)?.path ?? null;
    return candIsLinkedWorktree && mainPath !== null ? mainPath : null;
  }, [
    forkFreshArmed,
    seedWorktrees,
    seedWorktreesArePlaceholder,
    seedWorktreesErrored,
    autoSeedCandidate,
  ]);

  // Seed the working directory once per host, into an empty field only, so an
  // explicit pick isn't clobbered. Prefer the most-recent path; else the
  // derived home (which can arrive a render later, hence the dep). Holds
  // off while a project prefill is deciding on a workspace of its own.
  useEffect(() => {
    if (!prefillSettled) return;
    if (selectedHostId === null) return;
    if (seededHostRef.current === selectedHostId) return;
    if (autoSeedCandidate === null) return;
    // Fork-fresh redirect pending: wait for the probe rather than seeding the
    // wrong path (and locking the once-per-host guard).
    if (forkFreshMainPath === undefined) return;

    const didForkFresh = forkFreshMainPath !== null;
    const candidate = didForkFresh ? forkFreshMainPath : autoSeedCandidate;
    seededHostRef.current = selectedHostId;
    // Seed into an empty field only, so a config-supplied (or explicitly
    // picked) workspace isn't clobbered.
    const seededWorkspace = workspace === "";
    if (seededWorkspace) setWorkspace(candidate);
    // Fork fresh only when we actually seeded the redirect AND no branch is set
    // — a project that supplies its own workspace keeps a plain launch, and a
    // branch typed/picked while the probe was loading isn't overwritten (the
    // same guards the opt-in-worktree effect below enforces).
    if (didForkFresh && seededWorkspace && branchName === "" && prefilledBranch === "") {
      // Preempt the opt-in-worktree effect so it can't also seed a branch, then
      // name one here to fork fresh off the project default. Store the ref in
      // the raw representation that effect compares against (workspaceTrimmed).
      worktreeSeededForRef.current = candidate;
      generateBranchName();
    }
  }, [
    selectedHostId,
    autoSeedCandidate,
    prefillSettled,
    forkFreshMainPath,
    workspace,
    branchName,
    prefilledBranch,
    generateBranchName,
  ]);

  // A pick only wins while it exists in the list — a persisted id whose
  // agent has since been unregistered (or hidden) falls back to the default.
  // The pending custom agent sentinel also wins when set.
  // A pending (just-created, not-yet-submitted) custom agent can't run on a
  // managed sandbox — the sandbox create path doesn't provision a runner for a
  // bundled agent. So a pending pick made before switching to a sandbox is
  // dropped there, falling back to a real agent; off the sandbox it's kept.
  const pendingAgentAllowedOnTarget = !sandboxSelected;
  const effectiveAgentId =
    pickedAgentId === PENDING_AGENT_ID && pendingAgentAllowedOnTarget
      ? PENDING_AGENT_ID
      : ((agentList.some((a) => a.id === pickedAgentId) ? pickedAgentId : agentList[0]?.id) ??
        null);
  const selectedAgent = useMemo(
    () =>
      effectiveAgentId === PENDING_AGENT_ID && pendingAgent
        ? ({
            id: PENDING_AGENT_ID,
            name: pendingAgent.name,
            display_name: pendingAgent.name,
            description: pendingAgent.description ?? null,
            harness: pendingAgent.harness ?? null,
            skills: [],
          } satisfies AvailableAgent)
        : agentList.find((a) => a.id === effectiveAgentId),
    [agentList, effectiveAgentId, pendingAgent],
  );
  const supportsPermissionMode = nativeAgentHasCapability(selectedAgent, "permissionMode");
  const supportsApprovalMode = nativeAgentHasCapability(selectedAgent, "approvalMode");
  const supportsCursorMode = nativeAgentHasCapability(selectedAgent, "cursorMode");
  const hideUnconfiguredHarnesses = useMemo(() => readHideUnconfiguredHarnesses(), []);
  // The selected native harness, used to persist/seed its option knobs (mode /
  // model / effort), which are harness-specific. null for non-native agents,
  // which have no knobs to remember.
  const selectedNativeHarness = nativeCodingAgentForAvailableAgent(selectedAgent)?.harness ?? null;
  const selectedHost = allHosts.find((h) => h.host_id === selectedHostId);
  // Warn-only readiness signal for the agent picker: only meaningful when
  // a connected host is selected (a sandbox provisions its own tooling).
  // Selection stays allowed — the host re-checks at launch and the create
  // call surfaces a specific error if the harness really can't run.
  const harnessWarningHost = !sandboxSelected ? selectedHost : undefined;
  // Smart Routing as a Model choice is offered on the two native harnesses
  // whose running CLI accepts a per-turn model switch (the server injects
  // ``/model`` when cost_control_mode_override is "on"). Everything else routes
  // via the fully-auto harness instead, which picks harness + model up front.
  // Each family gates on its OWN source: the external router's apply layer
  // rewrites the model through the workspace AI gateway, so a host whose Claude
  // Code runs off something else falls back to the built-in judge for that
  // family instead of losing the row — and loses it only when neither router
  // can answer.
  const smartRoutingEligible =
    smartRoutingEnabled &&
    selectedNativeHarness !== null &&
    SMART_ROUTING_ARMS.some((harness) => harness === selectedNativeHarness) &&
    smartRoutingSourceFor({
      externalConfigured: externalRoutingConfigured,
      ossConfigured: ossRoutingConfigured,
      gatewayBacked: hostBacksHarnessWithGateway(harnessWarningHost, selectedNativeHarness),
    }) !== null;
  // Top-level Smart Routing (the "Harnesses" row, no bundle agent): the router
  // picks native Claude Code or Codex per task. It rides a placeholder wrapper
  // agent for the create call, so the pick lives in pickedHarness alone.
  const smartRoutingHarnessSelected = pickedHarness === AUTO_NATIVE_HARNESS_ID;
  // Whether the gear config modal has anything to show for the selected agent
  // (drives the gear icon's visibility). Bundle agents with an overridable
  // brain harness qualify, as does any routing-eligible agent — Smart Routing
  // lives only in the modal, so an agent with just that still needs the gear.
  const selectedAgentHasKnobs =
    supportsPermissionMode ||
    supportsApprovalMode ||
    supportsCursorMode ||
    smartRoutingEligible ||
    (selectedAgent?.harness != null && selectedAgent.harness in brainHarnessLabelsAll);
  // Label/value pairs summarizing the selected agent's current run-config, for
  // the gear icon's hover tooltip. Mirrors the modal's per-capability rows so a
  // user can read the active settings without opening it. "Default" = an unset
  // model/effort (Claude Code uses its own configured default).
  // Gate on eligibility so a stale "on" (server flag off, or a non-routable
  // agent) never shows misleading routing rows in the tooltip.
  const routingOn = smartRoutingEligible && costControlMode === "on";
  // Either fully-auto flavor is active: the router owns harness + model, so the
  // per-turn routing rows don't apply. Row gating only — this says nothing about
  // WHOSE session it is. Identity readers (the composer chip, its tooltip, the
  // modal title) key on `smartRoutingHarnessSelected` instead, because a bundle
  // agent's routed brain is a knob on that agent, not a different selection.
  const autoRoutingSelected =
    smartRoutingHarnessSelected ||
    (pickedHarness === AUTO_HARNESS_ID &&
      selectedAgent?.harness != null &&
      selectedAgent.harness in brainHarnessLabelsAll);
  const configSummary = useMemo((): { label: string; value: string }[] => {
    if (smartRoutingHarnessSelected) {
      // Top-level Smart Routing's modal is the locked Permissions row alone, so
      // mirror it. Report the constant — never a mode left over in state from a
      // previously selected native harness.
      return [{ label: "Permissions", value: AUTO_PERMISSION_MODE.label }];
    }
    if (supportsPermissionMode) {
      const modelValue = routingOn
        ? SMART_ROUTING_LABEL
        : (claudeModelOptions.find((m) => m.id === pickedModel)?.displayName ?? "Default");
      // Routing picks the model + effort per turn, so mirror the modal's frozen
      // Effort row: an em-dash when routing is on, else the picked level.
      const effortValue = routingOn
        ? EFFORT_UNAVAILABLE_PLACEHOLDER
        : !pickedEffort
          ? "Default"
          : (CLAUDE_NATIVE_EFFORTS.find((e) => e.value === pickedEffort)?.label ?? "Default");
      const permissionValue =
        CLAUDE_NATIVE_PERMISSION_MODES.find((m) => m.value === permissionMode)?.label ??
        permissionMode;
      return [
        { label: "Model", value: modelValue },
        { label: "Effort", value: effortValue },
        { label: "Permissions", value: permissionValue },
      ];
    }
    // Codex folds routing into its Model row, so report it the same way Claude
    // does above rather than as a separate toggle row.
    const routingRow: { label: string; value: string }[] = routingOn
      ? [{ label: "Model", value: SMART_ROUTING_LABEL }]
      : [];
    if (supportsApprovalMode) {
      const isCodex = nativeCodingAgentForAvailableAgent(selectedAgent)?.harness === "codex-native";
      // Bypass is the most-permissive Approval choice, not a separate knob — so
      // mirror the modal's single Approval control: when armed, the Approval row
      // reads "Bypass approvals & sandbox" rather than the underlying preset
      // (which would misleadingly imply approvals are still at e.g. "Default").
      const approvalValue =
        isCodex && bypassSandbox
          ? CODEX_NATIVE_BYPASS_APPROVAL_OPTION.label
          : (CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.label ??
            approvalMode);
      const modelRows =
        routingOn || !isCodex
          ? routingRow
          : [
              {
                label: "Model",
                value:
                  codexModelOptions.find((m) => m.id === pickedModel)?.id ??
                  defaultModelLabel(codexModelOptions, displayModelId),
              },
            ];
      return [...modelRows, { label: "Approval", value: approvalValue }];
    }
    if (supportsCursorMode) {
      const modeValue =
        CURSOR_NATIVE_EXEC_MODES.find((m) => m.value === cursorExecMode)?.label ?? cursorExecMode;
      return [{ label: "Mode", value: modeValue }, ...routingRow];
    }
    if (selectedAgent?.harness != null && selectedAgent.harness in brainHarnessLabelsAll) {
      const active = pickedHarness ?? selectedAgent.harness;
      return [
        { label: "Agent Harness", value: brainHarnessLabelsAll[active] ?? active },
        ...routingRow,
      ];
    }
    return routingRow;
  }, [
    smartRoutingHarnessSelected,
    supportsPermissionMode,
    supportsApprovalMode,
    supportsCursorMode,
    selectedAgent,
    brainHarnessLabelsAll,
    routingOn,
    pickedModel,
    claudeModelOptions,
    codexModelOptions,
    pickedEffort,
    permissionMode,
    approvalMode,
    bypassSandbox,
    cursorExecMode,
    pickedHarness,
  ]);
  // Reset per-agent-instance run-config that must not carry across an agent
  // change. The DANGEROUS Codex bypass re-opts-in per context (matching the
  // store's fork / agent-switch behavior; CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY
  // is instance-scoped). Smart routing likewise clears: switching to an agent
  // whose modal has no routing control (or isn't routable) would otherwise
  // leave it stuck "on" with no UI to turn it off.
  //
  // Only reset on an ACTUAL agent change — not the initial resolution (null →
  // first id, or a persisted/draft pick resolving on mount), which would wipe a
  // costControlMode/bypass restored from the landing draft.
  const prevAgentIdRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const prev = prevAgentIdRef.current;
    prevAgentIdRef.current = effectiveAgentId;
    if (prev === undefined || prev === effectiveAgentId) return;
    setBypassSandbox(false);
    setCostControlMode(null);
  }, [effectiveAgentId, setCostControlMode]);
  // Seed the harness's knobs from the user's last picks when the selected
  // harness changes (including the first mount), so a returning user starts a
  // new session on the options they used last for that harness instead of the
  // default. Keyed on the harness so an in-session edit isn't clobbered on
  // re-render — only a harness switch reseeds.
  useEffect(() => {
    if (!selectedNativeHarness) return;
    const stored = readHarnessOptions(selectedNativeHarness);
    // Resolve the mode to the stored value when it's still valid for this
    // harness, else the harness default. The else branch must RESET (not
    // early-return) because codex-native and opencode-native share the single
    // approvalMode state: returning early would leave the previously-selected
    // harness's mode in place — e.g. codex's "full-access" carried onto
    // OpenCode — and flow into the launch args unchanged. A stale value not in
    // the current list resolves to the default for the same reason.
    const resolve = (modes: readonly { value: string }[], dflt: string) =>
      stored.mode != null && modes.some((m) => m.value === stored.mode) ? stored.mode : dflt;
    // A remembered "route every turn" outranks a remembered concrete model: the
    // two are mutually exclusive, and sending both makes the server treat the
    // session as model-pinned and never route. Read from storage (not state) so
    // this holds on every run of this effect — including the re-run when the
    // model catalog resolves, which lands after the routing seed below.
    const storedRoutingOn = stored.routing === "on";
    if (supportsPermissionMode) {
      setPermissionMode(
        resolve(CLAUDE_NATIVE_PERMISSION_MODES, CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE),
      );
      // The model + effort picker remembers its own last pick (same per-harness
      // snapshot the mode knob uses), validated against the current vocab. With
      // nothing stored (or a retired id) it resolves to "" — unselected, so the
      // create omits the override and Claude Code uses its own configured model.
      setPickedModel(
        !storedRoutingOn &&
          stored.model != null &&
          claudeModelOptions.some((m) => m.id === stored.model)
          ? stored.model
          : "",
      );
      setPickedEffort(
        !storedRoutingOn &&
          stored.effort != null &&
          CLAUDE_NATIVE_EFFORTS.some((e) => e.value === stored.effort)
          ? stored.effort
          : "",
      );
    } else if (supportsApprovalMode) {
      setApprovalMode(resolve(CODEX_NATIVE_APPROVAL_MODES, CODEX_NATIVE_DEFAULT_APPROVAL_MODE));
      // A remembered routing "on" outranks a remembered concrete model, and
      // also drops any model/effort left in the shared state (e.g. seeded for
      // Claude Code before the harness switch).
      setPickedModel(
        !storedRoutingOn &&
          selectedNativeHarness === "codex-native" &&
          stored.model != null &&
          codexModelOptions.some((m) => m.id === stored.model)
          ? stored.model
          : "",
      );
      if (storedRoutingOn) setPickedEffort("");
    } else if (supportsCursorMode) {
      setCursorExecMode(resolve(CURSOR_NATIVE_EXEC_MODES, CURSOR_NATIVE_DEFAULT_EXEC_MODE));
    }
    // Reseed on harness changes and when the selected host's catalog resolves;
    // capability flags are derived from the same harness and stay omitted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedNativeHarness, claudeModelOptions, codexModelOptions]);
  // Smart Routing is remembered per harness alongside the mode/model
  // knobs, in its own effect because eligibility depends on the server flag
  // (which resolves after mount — this must reseed when it lands). A stored
  // "on" on a server without routing resolves to Default, so the create sends
  // no override. Nothing stored → leave the current value alone, so a restored
  // landing draft isn't downgraded.
  // Keyed on the agent too (not just the harness) so it re-runs after the
  // agent-change reset above, which clears routing for every agent switch —
  // including one between two agents on the same harness. Fully-auto owns the
  // switch itself (the router always routes), so it's left alone.
  useEffect(() => {
    if (!selectedNativeHarness || autoRoutingSelected) return;
    const storedRouting = readHarnessOptions(selectedNativeHarness).routing;
    if (storedRouting === undefined) return;
    setCostControlMode(smartRoutingEligible && storedRouting === "on" ? "on" : null);
  }, [
    selectedNativeHarness,
    smartRoutingEligible,
    effectiveAgentId,
    autoRoutingSelected,
    setCostControlMode,
  ]);
  // Top-level Smart Routing pins permissions to Default (no override sent), so
  // entering it resets the mode rather than restoring one: nothing is remembered
  // for the sentinel, and a value left over from a previously selected native
  // harness must not ride along into the router's pick. The bundle-agent flavor
  // is untouched — its create call (claude-sdk) carries no permission field, and
  // resetting would clobber the mode of whatever native harness comes next.
  useEffect(() => {
    if (pickedHarness !== AUTO_NATIVE_HARNESS_ID) return;
    setPermissionMode(CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE);
  }, [pickedHarness]);
  // Native-terminal agents interpret slash commands inside their own CLI
  // (the runner injects the text verbatim), so the landing composer must
  // not intercept them — no skills menu, no slash_command routing.
  const isNativeTerminalAgent = isNativeCodingAgent(selectedAgent);
  const selectedAgentUnconfigured = harnessUnconfiguredOnHost(
    selectedAgent?.harness,
    harnessWarningHost,
  );
  // Smart Routing routes between native Claude Code and Codex, so both wrapper
  // agents must be registered and both CLIs ready on the target host — a router
  // with one arm is just that arm. The Claude wrapper is the placeholder the
  // create binds; the server rebinds to whichever the router picks.
  const smartRoutingWrappers = useMemo(() => {
    const byHarness = (harness: string) =>
      harnessEntries.find((a) => nativeCodingAgentForAvailableAgent(a)?.harness === harness);
    return {
      claude: byHarness("claude-native"),
      codex: byHarness("codex-native"),
    };
  }, [harnessEntries]);
  // Why Smart Routing can't be offered right now, or null when it can. The
  // notice below quotes this, so "unavailable" is never reported as the wrong
  // cause (a server with routing off is not a host missing a CLI).
  const smartRoutingUnavailableCause = useMemo(
    (): SmartRoutingUnavailableCause | null =>
      smartRoutingUnavailableReason({
        routingEnabled: smartRoutingEnabled,
        wrappersRegistered:
          smartRoutingWrappers.claude != null && smartRoutingWrappers.codex != null,
        unreadyHarnesses: SMART_ROUTING_ARMS.filter((harness) =>
          harnessUnconfiguredOnHost(harness, harnessWarningHost),
        ),
        // Picking the harness is the external router's job alone, so the row
        // needs it configured AND both families on the gateway its apply layer
        // rewrites through. The built-in judge routes a model inside one
        // harness and can't stand in here.
        externalRoutingAvailable: externalRoutingConfigured,
        notGatewayBackedHarnesses: SMART_ROUTING_ARMS.filter(
          (harness) => !hostBacksHarnessWithGateway(harnessWarningHost, harness),
        ),
      }),
    [smartRoutingEnabled, smartRoutingWrappers, harnessWarningHost, externalRoutingConfigured],
  );
  const smartRoutingHarnessAvailable = smartRoutingUnavailableCause === null;
  // The fully-auto brain needs SOME router able to answer for both model
  // families — the router may land the session's work on either, and an arm the
  // external router can't reach (off the workspace AI gateway) is only a loss
  // when the built-in judge can't cover it either. The judge picks the bundle
  // brain's harness as well as its model, so unlike the native-pane row above
  // this surface stays on a judge-only deployment. Source availability ONLY:
  // the bundle brain routes across SDK harnesses, so the native wrappers/CLIs
  // are deliberately not required here. Gates the OPTIONS map only — membership
  // checks and the summary label for an existing pick keep reading
  // `brainHarnessLabelsAll`.
  const brainRoutable = SMART_ROUTING_ARMS.every(
    (harness) =>
      smartRoutingSourceFor({
        externalConfigured: externalRoutingConfigured,
        ossConfigured: ossRoutingConfigured,
        gatewayBacked: hostBacksHarnessWithGateway(harnessWarningHost, harness),
      }) !== null,
  );
  const brainHarnessLabels = useMemo(() => {
    if (brainRoutable) return brainHarnessLabelsAll;
    const { [AUTO_HARNESS_ID]: _dropped, ...rest } = brainHarnessLabelsAll;
    return rest;
  }, [brainHarnessLabelsAll, brainRoutable]);
  // Whether we know enough to judge availability: before the agent list, the
  // server flags, and the target (host or sandbox) land, "unavailable" only
  // means "not loaded yet". The target matters as much as the rest — with no
  // host resolved the arms read as ready, so judging early would report the
  // auto-selected host's own arms as a loss the user caused.
  const smartRoutingAvailabilityKnown =
    agents !== undefined &&
    info !== "loading" &&
    !hostsLoading &&
    (sandboxSelected || selectedHost !== undefined || allHosts.length === 0);
  // A restored (or newly unsupported) Smart Routing pick with no row behind it —
  // routing disabled server-side, or either native arm missing on this host —
  // would strand a "Smart Routing" chip the user can't switch away from. Drop
  // back to the default pick, as if nothing had been stored; the stored value
  // stays put, since the arm may come back.
  // Silently swapping a pick the user just made reads as the UI forgetting it,
  // so a loss of availability *while the landing is open* (a host switch) is
  // announced in the harness-readiness slot. A pick restored from localStorage
  // onto a host that never had routing is dropped quietly: the user did nothing
  // to lose it, and the row they'd be told about isn't in the picker either.
  const [smartRoutingDropped, setSmartRoutingDropped] =
    useState<SmartRoutingUnavailableCause | null>(null);
  const routingWasAvailable = useRef(false);
  useEffect(() => {
    if (!smartRoutingAvailabilityKnown) return;
    const wasAvailable = routingWasAvailable.current;
    routingWasAvailable.current = smartRoutingHarnessAvailable;
    if (!smartRoutingHarnessSelected || smartRoutingHarnessAvailable) return;
    setPickedHarness(null);
    _setCostControlMode(null);
    if (wasAvailable) setSmartRoutingDropped(smartRoutingUnavailableCause);
  }, [
    smartRoutingHarnessSelected,
    smartRoutingAvailabilityKnown,
    smartRoutingHarnessAvailable,
    smartRoutingUnavailableCause,
  ]);
  // Routing came back (host switched again, or the missing arm installed) —
  // there is nothing left to explain. An explicit pick clears it too, from the
  // pick handlers (a state-derived clear would race the drop's own re-render).
  useEffect(() => {
    if (smartRoutingHarnessAvailable) setSmartRoutingDropped(null);
  }, [smartRoutingHarnessAvailable]);
  // Same degrade for the bundle-agent flavor: a remembered fully-auto brain pick
  // on Polly / Debby has no row behind it once the server switches routing off,
  // so the harness select would show a blank value with no way back while the
  // create still sent harness_override "auto". Quiet, like the top-level flavor —
  // the user did nothing this visit to lose it, and the stored pick stays put in
  // case routing returns.
  useEffect(() => {
    if (info === "loading" || smartRoutingEnabled) return;
    if (pickedHarness !== AUTO_HARNESS_ID) return;
    setPickedHarness(null);
    _setCostControlMode(null);
  }, [info, smartRoutingEnabled, pickedHarness]);
  const workspaceTrimmed = workspace.trim();
  const workspaceValid = isValidWorkspace(workspace);
  const isCloudHost =
    sandboxSelected || (selectedHost?.name?.toLowerCase().includes("cloud") ?? false);

  // Sessions on the selected host that have a workspace — the narrow set
  // the health poll needs to check for live directory conflicts. Much
  // smaller than all 200 directorySessions (only host-matched + workspace
  // rows), so registering them into the /health poll is cheap.
  const conflictCandidates = useMemo(
    () =>
      (directorySessions ?? []).filter((s) => s.host_id === selectedHostId && s.workspace != null),
    [directorySessions, selectedHostId],
  );
  const runnerHealth = useRunnerHealthRegistration(conflictCandidates);
  // Count of live agents per normalized directory on this host. The file
  // browser uses this to warn when you navigate into an occupied directory.
  const occupancyByDir = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of conflictCandidates) {
      if (s.workspace == null || runnerHealth.get(s.id) !== true) continue;
      const dir = normalizeWorkspacePath(s.workspace);
      if (dir === null) continue;
      counts.set(dir, (counts.get(dir) ?? 0) + 1);
    }
    return counts;
  }, [conflictCandidates, runnerHealth]);

  // Existing git worktrees of the picked directory's repo, for the
  // worktree picker. Skipped for sandbox sessions (server-managed) and
  // when no directory is picked. A non-git path resolves to [].
  const worktreesEnabled = !sandboxSelected && selectedHostId !== null && workspaceTrimmed !== "";
  const { data: hostWorktrees, isPlaceholderData: hostWorktreesArePlaceholder } = useHostWorktrees(
    worktreesEnabled ? selectedHostId : null,
    worktreesEnabled ? workspaceTrimmed : null,
  );
  // Linked worktrees (exclude the main work tree — "starting in the main
  // repo" is just picking that directory, not selecting a worktree).
  const linkedWorktrees = useMemo(
    () => (hostWorktrees ?? []).filter((w) => !w.is_main),
    [hostWorktrees],
  );
  // The worktree the picked directory currently points at, if any. Set when
  // the user navigated the picker straight into a worktree folder, or clicked
  // one in the list below.
  const activeWorktree = useMemo(() => {
    const target = normalizeWorkspacePath(workspaceTrimmed);
    if (target === null) return null;
    return linkedWorktrees.find((w) => normalizeWorkspacePath(w.path) === target) ?? null;
  }, [linkedWorktrees, workspaceTrimmed]);
  // When the workspace lands on an existing worktree, prefill the branch
  // field with its branch and remember it as the prefill. Leaving the
  // worktree clears the prefill (but not a name the user typed themselves).
  useEffect(() => {
    const branch = activeWorktree?.branch ?? "";
    if (branch !== "") {
      setPrefilledBranch(branch);
      setBranchName(branch);
    } else {
      setPrefilledBranch((prev) => {
        // Only clear the field if it still holds the previous prefill —
        // don't wipe a branch name the user typed for a new worktree.
        setBranchName((cur) => (cur === prev ? "" : cur));
        return "";
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeWorktree?.path]);
  // True when the session should start directly in the existing worktree:
  // the workspace is a worktree and the branch field still holds its
  // prefilled branch (the user hasn't edited it to request a new worktree).
  const startInExistingWorktree =
    activeWorktree !== null && prefilledBranch !== "" && branchName.trim() === prefilledBranch;
  // A new, isolated worktree is created only when a branch is named and the
  // workspace isn't already sitting on that existing worktree.
  const shouldCreateWorktree = branchName.trim() !== "" && !startInExistingWorktree;
  // Auto-fill the base branch when a new-worktree branch is named, but only
  // until the user touches the base field — then their choice (including a
  // cleared field) stands. Clearing the branch name (so the base field goes
  // away) re-arms the auto-fill, so naming a branch again starts fresh from the
  // current default. The project's stored default (Project settings) wins over
  // the user-global one (Settings › Git); an unset project default falls
  // through to the global one, then to blank (fork from current branch).
  useEffect(() => {
    if (!shouldCreateWorktree) {
      // No base field shown: reset so the next named branch re-seeds cleanly.
      setBaseBranchEdited(false);
      _setBaseBranch("");
      return;
    }
    if (!baseBranchEdited) {
      _setBaseBranch(projectBaseBranch ?? readDefaultBaseBranch() ?? "");
    }
  }, [shouldCreateWorktree, baseBranchEdited, projectBaseBranch]);
  // The branch input doubles as a combobox: focusing it reveals existing
  // worktrees, and what the user types filters them (match on branch or path
  // substring, case-insensitive). Typing a name that matches none = a new
  // worktree; picking a match = start in that existing worktree.
  const [branchInputFocused, setBranchInputFocused] = useState(false);
  const filteredWorktrees = useMemo(() => {
    const q = branchName.trim().toLowerCase();
    if (q === "") return linkedWorktrees;
    return linkedWorktrees.filter(
      (w) => (w.branch ?? "").toLowerCase().includes(q) || w.path.toLowerCase().includes(q),
    );
  }, [linkedWorktrees, branchName]);
  // Project prefill: seed host / workspace / agent from the project's stored
  // config, then settle so the generic defaults fill any slot the config left
  // unset. An opt-in worktree is generated by the dedicated effect below once
  // the workspace is in place.
  useEffect(() => {
    if (prefill.project !== projectParam || prefillDone(prefill)) return;
    const step = projectPrefillStep(prefill, {
      hosts,
      // The pickable list, not the raw one — a hidden agent's id would seed
      // a pick that effectiveAgentId rejects. Raw undefined = still loading.
      agents: agents === undefined ? undefined : agentList,
      sandboxSelected,
      managedSandboxesEnabled,
      selectedHostId,
      lastAgentId: readLastAgentId(),
      config: prefillConfig,
    });
    if (step === null) return;
    const { writes } = step;
    if (writes.selectSandbox) setSandboxSelected(true);
    if (writes.hostId !== undefined) setSelectedHostId((cur) => cur ?? writes.hostId!);
    if (writes.agentId !== undefined) {
      setPickedAgentId((cur) => cur ?? writes.agentId!);
      if (pickedAgentId === null) setPickedHarness(readLastHarness(writes.agentId));
    }
    if (writes.workspace !== undefined) {
      setWorkspace((cur) => (cur === "" ? writes.workspace! : cur));
    }
    setPrefill(step.state);
  }, [
    prefill,
    projectParam,
    hosts,
    agents,
    agentList,
    sandboxSelected,
    managedSandboxesEnabled,
    selectedHostId,
    pickedAgentId,
    prefillConfig,
  ]);

  // Opt-in worktree from the project's stored config. The inference machine
  // settles a config-driven location without touching the branch, so this
  // effect creates the fresh worktree once the workspace is fully in place —
  // whether it came from the config's own workspace or the composer's
  // home-fallback (which runs after the machine settles). Fires at most once
  // per settled workspace (ref-guarded) and only into an empty branch, so a
  // typed branch / existing-worktree prefill is never clobbered.
  useEffect(() => {
    if (prefillConfig?.useWorktree !== true) return;
    if (prefill.project !== projectParam || !prefillDone(prefill)) return;
    if (sandboxSelected || selectedHostId === null || workspaceTrimmed === "") return;
    if (branchName !== "" || prefilledBranch !== "") return;
    if (worktreeSeededForRef.current === workspaceTrimmed) return;
    // Need the git-ness probe for the CURRENT workspace resolved (not the
    // anti-flicker placeholder from a previous path).
    if (hostWorktreesArePlaceholder || hostWorktrees === undefined) return;
    worktreeSeededForRef.current = workspaceTrimmed;
    if (hostWorktrees.some((w) => w.is_main)) generateBranchName();
  }, [
    prefillConfig,
    prefill,
    projectParam,
    sandboxSelected,
    selectedHostId,
    workspaceTrimmed,
    branchName,
    prefilledBranch,
    hostWorktrees,
    hostWorktreesArePlaceholder,
    generateBranchName,
  ]);

  // Sandbox repo inputs are valid when blank (empty workspace), or when
  // the URL passes the shape check; a branch without a URL is dangling.
  const sandboxRepoValid =
    sandboxRepoUrl.trim() === ""
      ? sandboxRepoBranch.trim() === ""
      : isValidSandboxRepoUrl(sandboxRepoUrl);

  // Sandbox creates need no host or path workspace — the server
  // provisions both; only the message, agent, and (optional) repo
  // inputs gate the submit.
  // Slash-command suggestions for the chosen agent's bundled skills.
  // Mirrors the in-session composer's menu mechanics (open while the
  // command name is still being typed: leading "/", no second "/", no
  // space yet), but lists skills only — built-ins like /model need a
  // live session. Hidden for native-terminal agents (their CLI owns
  // slash commands) and for agents without bundled skills.
  const [slashMenuIndex, setSlashMenuIndex] = useState(-1);
  const skillCommands = useMemo(() => {
    if (isNativeTerminalAgent) return {};
    const m: Record<string, string> = {};
    for (const s of selectedAgent?.skills ?? []) m[`/${s.name}`] = s.description;
    return m;
  }, [selectedAgent, isNativeTerminalAgent]);
  const trimmedMessage = message.trimStart();
  const slashMenuOpen =
    trimmedMessage.startsWith("/") &&
    !trimmedMessage.slice(1).includes("/") &&
    !trimmedMessage.includes(" ");
  const slashMenuQuery = slashMenuOpen ? trimmedMessage.slice(1) : "";
  // Kept in sync with what SlashCommandMenu renders so keyboard nav
  // indexes into the same list.
  const slashMenuMatches = slashMenuOpen
    ? rankedSlashCommandNames(skillCommands, slashMenuQuery)
    : [];
  // Pre-select the first match whenever the filtered list changes, so
  // Tab/Enter complete the top item without arrowing down first (same
  // reset pattern as the in-session composer).
  const prevSlashMatchesRef = useRef<string[]>([]);
  if (
    slashMenuMatches.length !== prevSlashMatchesRef.current.length ||
    slashMenuMatches.some((m, i) => m !== prevSlashMatchesRef.current[i])
  ) {
    prevSlashMatchesRef.current = slashMenuMatches;
    setSlashMenuIndex(slashMenuMatches.length > 0 ? 0 : -1);
  }

  // Selecting a skill fills "/name " and leaves the caret ready for the
  // argument — skills never auto-execute from the menu.
  function applySlashSelection(cmd: string) {
    setSlashMenuIndex(-1);
    setMessage(cmd + " ");
    textareaRef.current?.focus();
  }

  // Always-visible skill pills for the allowlisted orchestrators, fed by
  // the same bundled-skills list as the "/" menu.
  const pillSkills =
    selectedAgent && SKILL_PILL_AGENTS.has(selectedAgent.name) ? selectedAgent.skills : [];

  // Pills only render over an empty draft, so there's never args to preserve.
  function applySkillPill(name: string) {
    setMessage(`/${name} `);
    textareaRef.current?.focus();
  }

  // ── "@"-file-mention browser (parity with the in-session composer) ────────
  // Only for native terminal agents on a real local host with an absolute
  // workspace. No session/runner exists yet, so the listing comes from the
  // host filesystem endpoint (absolute paths) rather than the session-scoped
  // workspace API; each tagged path is delivered as an "[Attached: …]" marker
  // prepended to the first message, which the runner reads from that workspace.
  const [mention, setMention] = useState<MentionState | null>(null);
  const mentionEnabled =
    isNativeTerminalAgent && !sandboxSelected && !!selectedHostId && workspaceValid;
  const { dir: mentionDir, filter: mentionFilter } = parseMentionToken(mention?.query ?? "");
  const workspaceRoot = workspaceTrimmed.replace(/\/+$/, "");
  // Absolute dir to list = workspace root + the drilled sub-path.
  const mentionAbsDir =
    mentionEnabled && mention
      ? mentionDir
        ? `${workspaceRoot}/${mentionDir}`
        : workspaceRoot
      : null;
  const mentionFsQuery = useHostFilesystem(
    mentionEnabled && mention ? selectedHostId : null,
    mentionAbsDir,
  );
  // Map host entries (absolute paths) to workspace-relative WorkspaceFile rows,
  // then rank (folders-first, filtered, capped) via the shared helper.
  const mentionEntries: WorkspaceFile[] = useMemo(() => {
    if (!mentionEnabled || !mention) return [];
    // ``useHostFilesystem`` keeps the previous directory's rows as placeholder
    // data (no flicker on navigate). When the user drills into a folder a new
    // fetch starts but ``data`` still holds the *parent's* entries — ``isLoading``
    // is false, only ``isPlaceholderData`` is true. Returning those stale rows
    // here would show the parent's files while purporting to be inside the
    // child, so a click/Enter could attach the wrong entry. Suppress them until
    // the current directory's own listing arrives.
    if (mentionFsQuery.isPlaceholderData) return [];
    const rows = (mentionFsQuery.data?.entries ?? [])
      .filter((e) => e.type === "directory" || e.type === "file")
      .map((e): WorkspaceFile => ({
        path: e.path.startsWith(workspaceRoot)
          ? e.path.slice(workspaceRoot.length).replace(/^\/+/, "")
          : e.name,
        name: e.name,
        type: e.type === "directory" ? "directory" : "file",
        bytes: e.bytes,
        modified_at: e.modified_at,
      }));
    return rankMentionEntries(rows, mentionFilter);
  }, [
    mentionEnabled,
    mention,
    mentionFsQuery.data,
    mentionFsQuery.isPlaceholderData,
    mentionFilter,
    workspaceRoot,
  ]);
  const mentionOpen = mentionEntries.length > 0;
  // Closed-but-loading window: don't let Enter send the half-typed "@dir/".
  // ``isPlaceholderData`` covers the drill-down window where react-query is
  // still serving the previous directory's rows (``isLoading`` stays false).
  const mentionListingPending =
    mentionEnabled &&
    mention != null &&
    (mentionFsQuery.isLoading || mentionFsQuery.isPlaceholderData);

  // Shared selection/chip/keyboard glue — see useMentionBrowser. Only the
  // host-filesystem source + token state above are launcher-specific.
  const {
    mentionIndex,
    mentionedItems,
    attachMention,
    openMentionDir,
    removeMentionedItem,
    handleKeyDown: handleMentionKeyDown,
    dismiss: dismissMention,
  } = useMentionBrowser({
    mention,
    setMention,
    mentionEntries,
    text: message,
    setText: setMessage,
    textareaRef,
  });

  const canSubmit =
    message.trim().length > 0 &&
    selectedAgent != null &&
    (sandboxSelected ? sandboxRepoValid : !!selectedHostId && workspaceValid) &&
    !creating;

  // Why submit is disabled, surfaced as the button's tooltip. Checked in the
  // order a user fills the form — location first, then message — so the
  // tooltip always names the next missing input. Null when nothing is
  // actionable (submitting, or mid-create).
  const submitDisabledReason = canSubmit
    ? null
    : sandboxSelected && !sandboxRepoValid
      ? "Please enter a valid repository URL"
      : !sandboxSelected && (!selectedHostId || !workspaceValid)
        ? "Please choose a host and working directory"
        : message.trim().length === 0
          ? "Enter a message to get started"
          : null;

  // Chip display labels.
  const workspaceLabel = workspaceTrimmed
    ? (workspaceTrimmed.split("/").filter(Boolean).pop() ?? workspaceTrimmed)
    : "Working directory";
  const selectedHostDisplayName = selectedHost
    ? displayNameForHost(selectedHost, thisMachineHostId, navigator.userAgent)
    : null;
  const hostLabel = connectingThisMachine
    ? "Connecting…"
    : sandboxSelected
      ? sandboxLabel
      : (selectedHostDisplayName ?? (onlineHosts.length === 0 ? "No hosts" : "Choose host"));
  // The chip shows just the branch (the "(existing)" distinction lives in the
  // popover's warning; appending it here only gets clipped by the chip's cap).
  const worktreeLabel = branchName.trim() || "Worktree";
  // Sandbox repository chip label: repo name (server's clone-dir rule)
  // plus the pinned branch, e.g. "repo#main"; placeholder when unset.
  const sandboxRepoName = deriveRepoName(sandboxRepoUrl);
  const sandboxRepoLabel = sandboxRepoName
    ? sandboxRepoBranch.trim()
      ? `${sandboxRepoName}#${sandboxRepoBranch.trim()}`
      : sandboxRepoName
    : "Repository";
  // The trigger label is just the agent name; the run-config knobs live in
  // the picker's per-entry submenu, so duplicating their values here would be
  // redundant. Top-level Smart Routing is the exception: it has no agent of its
  // own (the wrapper it binds is a placeholder), so naming that wrapper would
  // misreport what runs. A bundle agent whose brain is routed still runs as that
  // agent, so the chip keeps naming it — the routed brain is a knob, not a
  // different selection.
  const agentLabel = smartRoutingHarnessSelected
    ? SMART_ROUTING_LABEL
    : selectedAgent
      ? selectedAgent.display_name
      : "Select agent";

  // Wrap the harness setter so every explicit pick is persisted to
  // localStorage. The caller can pass an explicit `agentId` for the
  // switch-via-submenu path where `effectiveAgentId` still reflects the
  // previously selected agent (the state update from `onSelectAgent` hasn't
  // applied yet).
  const handleSetPickedHarness = useCallback(
    (harness: string | null, agentId?: string) => {
      setSmartRoutingDropped(null);
      setPickedHarness(harness);
      writeLastHarness(agentId ?? effectiveAgentId, harness);
      // Light up routing when either Auto Harness flavor is picked (both route
      // harness + model); off otherwise.
      _setCostControlMode(isAutoHarness(harness) ? "on" : null);
    },
    [effectiveAgentId],
  );

  // Pick top-level Smart Routing. The create call needs a concrete agent_id, so
  // bind the Claude wrapper as a placeholder — the server routes from the first
  // message and rebinds to the wrapper it picked, which is why the picker
  // suppresses that row's highlight while this sentinel is active.
  // Persisted as the placeholder's last harness, like every other harness pick,
  // so a return visit starts on Smart Routing again. A restored sentinel with no
  // row behind it degrades to the default pick (see the guard above).
  const handleSelectSmartRoutingHarness = () => {
    setSmartRoutingDropped(null);
    const placeholder = smartRoutingWrappers.claude;
    if (placeholder == null) return;
    setPickedAgentId(placeholder.id);
    writeLastAgentId(placeholder.id);
    setPickedHarness(AUTO_NATIVE_HARNESS_ID);
    writeLastHarness(placeholder.id, AUTO_NATIVE_HARNESS_ID);
    _setCostControlMode("on");
  };

  // Select an agent/harness from the picker. Switching agents seeds the
  // harness override from the user's last pick for that agent (so a
  // returning user lands on the harness they used last); explicit picks
  // persist via localStorage.
  const handleSelectAgent = (agent: AvailableAgent) => {
    setSmartRoutingDropped(null);
    if (agent.id !== effectiveAgentId) {
      const remembered = readLastHarness(agent.id);
      // Smart Routing is stored under the wrapper it binds as a placeholder, but
      // clicking that wrapper's own row is a pick of the wrapper — clear the
      // sentinel so the explicit choice is what survives a reload.
      if (remembered === AUTO_NATIVE_HARNESS_ID) handleSetPickedHarness(null, agent.id);
      else setPickedHarness(remembered);
    }
    // Re-picking the agent that top-level Smart Routing binds as its placeholder
    // drops back to that wrapper's own harness — the only way out, since that
    // modal has no harness row. A bundle agent's routed brain is deliberately
    // NOT cleared here: it is a saved knob on the agent, and its modal's
    // always-rendered Agent Harness row is how the user switches away.
    else if (pickedHarness === AUTO_NATIVE_HARNESS_ID) handleSetPickedHarness(null, agent.id);
    setPickedAgentId(agent.id);
    writeLastAgentId(agent.id);
  };
  const handleSelectPending = () => {
    setPickedAgentId(PENDING_AGENT_ID);
    setPickedHarness(null);
  };

  function selectHost(hostId: string) {
    // Persist the explicit pick even when it matches the current selection, so
    // clicking the auto-selected host still records it as the sticky default
    // for the next visit.
    writeLastHostChoice(hostId);
    // Re-selecting the current host is a no-op. Clearing the workspace here
    // would empty the field for good: the seeding effect's deps (host id,
    // recents, derived home) are all unchanged on a same-host pick, so it
    // never re-runs to fill the field back in — and a host the user already
    // has selected (e.g. the auto-picked first online host) is exactly the
    // one they're most likely to click in the menu.
    if (hostId === selectedHostId) return;
    setSandboxSelected(false);
    setSelectedHostId(hostId);
    // Workspace is host-specific — clear it and let the seeding effect run for
    // the new host.
    setWorkspace("");
    seededHostRef.current = null;
  }

  function selectSandbox() {
    // Persist the explicit sandbox pick (as the reserved sentinel) even when
    // it's already selected, mirroring selectHost — so the sandbox becomes the
    // sticky default for the next visit.
    writeLastHostChoice(SANDBOX_HOST_CHOICE);
    if (sandboxSelected) return;
    // Mirror selectHost: a managed session's host and workspace are both
    // server-chosen, so clear any prior host pick and its workspace.
    setSandboxSelected(true);
    setSelectedHostId(null);
    setWorkspace("");
    seededHostRef.current = null;
  }

  // Connect THIS desktop machine as a host for the current server, then select
  // it — so the user doesn't have to run `omni host` in a terminal first. The
  // bridge's controlHost resolves once the host is connected; we then read its
  // id, refresh the host list, and pick it.
  async function connectThisMachine() {
    if (connectingThisMachine) return;
    setConnectingThisMachine(true);
    try {
      const res = await controlHost("start");
      if (!res.ok) return;
      const identity = await getHostIdentity();
      setDesktopHost(identity);
      await queryClient.invalidateQueries({ queryKey: ["hosts"] });
      if (identity?.hostId) selectHost(identity.hostId);
    } finally {
      setConnectingThisMachine(false);
    }
  }

  // No session was created after all, so the draft is the user's again —
  // including when they navigated away and the unmount cleanup already
  // dropped it on the strength of the submit.
  function returnDraftToUser() {
    submittedRef.current = false;
    if (!onScreenRef.current) landingDraft = draftRef.current;
  }

  async function handleCreate() {
    // Mirror the Send button's disabled condition (canSubmit) so the Enter-key
    // and form-submit paths that call this directly can't create a session with
    // a blank message, host, agent, or workspace.
    if (!canSubmit) return;
    setCreating(true);
    setCreateError(null);
    // The draft is spent from the moment it is submitted: it belongs to the
    // session now being created, so a detour back to this screen must not
    // hand it back pre-filled. Flipped here rather than on the response
    // because the create outlives an unmount; a create that fails hands the
    // draft back via returnDraftToUser.
    submittedRef.current = true;
    try {
      const trimmedBranch = branchName.trim();
      // `shouldCreateWorktree` (component scope): true only when a branch is
      // named and the workspace isn't already an existing worktree. Starting
      // in an existing worktree sends no git opts — the workspace is bound
      // straight to that dir, which also sidesteps the "branch already
      // exists" guard.
      const agent = agentList.find((a) => a.id === effectiveAgentId);
      const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
      const nativeLabels = nativeWrapperLabelsForAgent(agent);
      const agentSupportsPermissionMode = nativeAgentHasCapability(agent, "permissionMode");
      const agentSupportsApprovalMode = nativeAgentHasCapability(agent, "approvalMode");
      const agentSupportsCursorMode = nativeAgentHasCapability(agent, "cursorMode");
      // Smart Routing — server-side. The fully-auto harness always routes
      // (harness + model), so send "on" to keep the persisted state consistent
      // with the lit routing icon. Otherwise only send it when routing is
      // eligible for the effective harness, so a stale "on" can't ride along
      // invisibly with no control to clear it.
      const costControlOverride =
        pickedHarness === AUTO_HARNESS_ID || smartRoutingHarnessSelected
          ? "on"
          : smartRoutingEligible
            ? (costControlMode ?? undefined)
            : undefined;
      // Belt and braces: the server routes a turn only while the session has no
      // pinned model ("on" plus no `model_override` is what makes it route), so
      // sending both would silently disable routing for the whole session. Never
      // pin a model or an effort alongside routing, whatever the UI state says.
      const routingOwnsModel = costControlOverride === "on";
      // A pinned native pane routes its MODEL at create too: the terminal
      // launches with the session row and its turns start in the TUI, so
      // routing after the fact means blocking the first prompt and replaying
      // it. Sending the prompt here pins `model_override` before the pane
      // exists. Bundle agents are excluded — they route on the first message
      // event by design.
      const pinnedNativeRoutes =
        routingOwnsModel &&
        !smartRoutingHarnessSelected &&
        SMART_ROUTING_ARMS.some((harness) => harness === nativeAgent?.harness);

      // Prepend each "@"-tagged path as an attachment marker on its own line —
      // the same wording the native executors emit and that title-seeding
      // strips. The runner, rooted at this workspace, reads the on-disk file
      // from the marker; no upload happens. Folders carry a trailing "/".
      // Computed BEFORE the create so `smart_routing_message` classifies the
      // prompt the agent actually receives, not the raw textarea value.
      const initialPrompt =
        buildMentionPreamble(mentionedItems, selectedAgent?.harness ?? null) +
        sanitizeInitialPrompt(message);

      // Native terminal agents open terminal-first: `omnigent.ui: terminal`
      // tells the UI to render the terminal wrapper, and `omnigent.wrapper`
      // selects which CLI bridge the runner launches — the values are the
      // registered wrapper ids the runner keys off, not the display name. The
      // DANGEROUS codex full-bypass opt-in rides along as an extra label (only
      // when the toggle is armed for a codex-native agent) so the runner
      // launches with --dangerously-bypass-approvals-and-sandbox and the choice
      // survives reload.
      const baseLabels =
        agentSupportsApprovalMode && bypassSandbox
          ? { ...(nativeLabels ?? {}), [CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY]: "1" }
          : nativeLabels;
      // When filing into a project, stamp its legacy `omni_project` label at
      // create so the session is BORN FILED. The sidebar dual-reads project
      // membership from this label OR the first-class `project_id` the follow-up
      // move sets, so the row groups under its project from its very first
      // sidebar appearance instead of flashing through the ungrouped "Sessions"
      // section while the search-indexed session list catches up to the move.
      const createLabels = selectedProject
        ? { ...(baseLabels ?? {}), [PROJECT_LABEL_KEY]: selectedProject }
        : baseLabels;

      let data: { id: string };

      if (effectiveAgentId === PENDING_AGENT_ID && pendingAgent) {
        // Custom agent path: build bundle client-side and use multipart POST.
        // The multipart create only stores the agent + session rows — it does
        // NOT launch a runner on the host. We must follow up with launchRunner
        // (POST /v1/hosts/{id}/runners) to bind the session to a runner, the
        // same way the fork-resume path does.
        const bundle = await buildAgentBundle(pendingAgent);
        const metadata: Record<string, unknown> = {};
        if (workspaceTrimmed) metadata.workspace = workspaceTrimmed;
        // Born-filed: stamp the project's `omni_project` label so a bundled
        // session groups under its project from its first sidebar appearance,
        // same as the JSON path (see `createLabels`).
        if (selectedProject) metadata.labels = { [PROJECT_LABEL_KEY]: selectedProject };
        data = await createBundledSession(
          bundle,
          metadata as Parameters<typeof createBundledSession>[1],
        );
        // Launch the runner on the selected host. The multipart create
        // only stores DB rows — launchRunner binds + starts the runner.
        if (!sandboxSelected && selectedHostId && workspaceTrimmed) {
          // Create a new worktree, bind an existing one (records the branch
          // for the sidebar + delete flow without creating anything), or
          // neither — mirrored on the `git` block.
          const gitOpts = shouldCreateWorktree
            ? { branchName: trimmedBranch, baseBranch: baseBranch.trim() || undefined }
            : startInExistingWorktree
              ? { branchName: trimmedBranch, existingWorktree: true }
              : undefined;
          await launchRunner(selectedHostId, data.id, workspaceTrimmed, gitOpts);
        }
        // Clear pending agent after successful creation.
        setPendingAgent(null);
      } else {
        // Normal path: bind to an existing registered agent.
        // Which pushed row is ours: the one this tab has never seen, bound
        // to the agent and host we're about to ask for. Sub-agent children
        // are never a create's result. Snapshotting the known ids BEFORE
        // the POST is what makes "never seen" mean "created by this call".
        const knownSessionIds = new Set(
          collectConversationIds(
            [
              ...queryClient.getQueriesData<ConversationsInfiniteData>({
                queryKey: ["conversations"],
              }),
              ...queryClient.getQueriesData<ConversationsInfiniteData>({
                queryKey: ["project-sessions"],
              }),
            ].map(([, cached]) => cached),
          ),
        );
        // A sandbox create has no host to match on until the sandbox
        // registers one, so it waits for the response like before.
        const matchOwnCreate =
          sandboxSelected || !selectedHostId
            ? null
            : (item: SessionListWireItem) =>
                !knownSessionIds.has(item.id) &&
                item.parent_session_id == null &&
                item.agent_id === effectiveAgentId &&
                item.host_id === selectedHostId;
        const createRequest = authenticatedFetch("/v1/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_id: effectiveAgentId,
            ...(sandboxSelected
              ? {
                  host_type: "managed",
                  workspace: composeSandboxWorkspace(sandboxRepoUrl, sandboxRepoBranch),
                }
              : {
                  host_id: selectedHostId,
                  workspace: workspaceTrimmed,
                  // Create a new worktree, or bind an existing one
                  // (`existing_worktree` records the branch for the sidebar +
                  // delete flow without creating anything), or neither.
                  git: shouldCreateWorktree
                    ? { branch_name: trimmedBranch, base_branch: baseBranch.trim() || undefined }
                    : startInExistingWorktree
                      ? { branch_name: trimmedBranch, existing_worktree: true }
                      : undefined,
                }),
            // Native-wrapper labels + codex bypass + the born-filed project
            // label (see `createLabels` above).
            // Smart Routing sends none of these: the bound agent is only a
            // placeholder, so the placeholder's wrapper labels, launch args and
            // model would all describe a CLI the router may not pick. The
            // server stamps the routed wrapper's labels once it has rebound.
            labels: smartRoutingHarnessSelected ? undefined : createLabels,
            // Permission / approval / cursor mode → CLI flag pair, persisted as
            // terminal_launch_args. Omitted for the default and non-native agents.
            terminal_launch_args: smartRoutingHarnessSelected
              ? undefined
              : agentSupportsPermissionMode &&
                  permissionMode !== CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
                ? ["--permission-mode", permissionMode]
                : agentSupportsApprovalMode && approvalMode !== CODEX_NATIVE_DEFAULT_APPROVAL_MODE
                  ? (CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.args ?? [])
                  : agentSupportsCursorMode && cursorExecMode !== CURSOR_NATIVE_DEFAULT_EXEC_MODE
                    ? (CURSOR_NATIVE_EXEC_MODES.find((m) => m.value === cursorExecMode)?.args ?? [])
                    : undefined,
            // Model + reasoning effort, persisted on the session row before
            // the runner launches. Claude and Codex read model_override at
            // terminal launch; an unselected ("") knob is omitted so the
            // harness keeps its own configured/default model.
            model_override:
              !smartRoutingHarnessSelected &&
              !routingOwnsModel &&
              (agentSupportsPermissionMode || nativeAgent?.harness === "codex-native") &&
              pickedModel
                ? pickedModel
                : undefined,
            reasoning_effort:
              !smartRoutingHarnessSelected &&
              !routingOwnsModel &&
              agentSupportsPermissionMode &&
              pickedEffort
                ? pickedEffort
                : undefined,
            cost_control_mode_override: costControlOverride,
            // Top-level Smart Routing sends the same "auto" sentinel the bundle
            // path does; the server tells them apart by the bound agent being a
            // native wrapper, and routes at create time (the terminal launches
            // with the row, so there is no first message to wait for). The
            // message text rides along for routing only — the client still
            // delivers the real message after navigation.
            harness_override: smartRoutingHarnessSelected
              ? AUTO_HARNESS_ID
              : (pickedHarness ?? undefined),
            smart_routing_message:
              smartRoutingHarnessSelected || pinnedNativeRoutes ? initialPrompt : undefined,
          }),
        });
        // The create doesn't answer until the host has spawned a runner — a
        // process boot, seconds of it — but the session row exists (and is
        // announced on the updates stream) almost immediately. Open the chat
        // on whichever id lands first: the pushed row typically wins by
        // seconds, and the chat page renders from the id alone, showing its
        // own starting spinner while the runner comes up.
        const abortPush = new AbortController();
        const pushedRow =
          matchOwnCreate === null
            ? Promise.resolve(null)
            : nextPushedSession(matchOwnCreate, abortPush.signal);
        const confirmed = (async (): Promise<{ id: string } | { error: string }> => {
          const response = await createRequest;
          if (!response.ok) return { error: await describeCreateError(response) };
          return { id: ((await response.json()) as { id: string }).id };
        })();
        // Once the create answers, its id is authoritative — stop listening.
        void confirmed.finally(() => abortPush.abort()).catch(() => {});
        const created = await new Promise<{ id: string } | { error: string }>((resolve, reject) => {
          // Only a match settles this; an abort resolves null and leaves the
          // response to decide.
          void pushedRow.then((row) => {
            if (row !== null) resolve({ id: row.id });
          });
          confirmed.then(resolve, reject);
        });
        // A row is only written (and announced) after the create has validated
        // the workspace and agent, so winning on the push can't skip past an
        // error the user needed to see on this screen.
        if ("error" in created) {
          returnDraftToUser();
          setCreateError(created.error);
          return;
        }
        data = { id: created.id };
      }
      // Promote the born-filed session to first-class project membership. The
      // create above already stamped the `omni_project` label (so the row
      // groups under its project immediately); this move sets the first-class
      // `project_id` and clears that label — the single source of truth after
      // the dual-read transition. Non-fatal if it fails: the session stays
      // filed by its label, so it still shows under the project either way.
      if (selectedProject) {
        try {
          // File via first-class project_id; the helper resolves the picked
          // name to a project id, creating an empty project on demand when the
          // name is new or label-only.
          await moveConversationToProject(data.id, selectedProject);
          void queryClient.invalidateQueries({ queryKey: ["projects"] });
          // Refetch the target project folder's own paginated list so the new
          // session shows up immediately (the folder fetches via
          // useProjectSessions, separate from the global conversations list).
          void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
        } catch {
          // Non-fatal: the create already stamped the `omni_project` label, so
          // the session stays filed under its project by label even if this
          // `project_id` promotion fails — the sidebar's dual-read grouping
          // still shows it under the project.
        }
      }
      // Sandbox creates have no user-picked workspace to remember.
      if (!sandboxSelected) addRecent(workspaceTrimmed);
      // Remember the launched harness so the picker promotes it out of "More"
      // next time. Recorded only on a successful create, so a harness the user
      // merely browsed past never earns a primary slot.
      if (selectedNativeHarness !== null) addRecentHarness(selectedNativeHarness);
      // Fire-and-forget: don't block navigation on the sidebar list refresh.
      // The background refetch (or the WS session_added push) backfills the
      // new session's row within ~1s of landing in the chat; the chat itself
      // loads from the session id and never reads the sidebar cache.
      void queryClient.refetchQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["directory-sessions"] });
      // A first message matching one of the agent's bundled skills is
      // handed off as a structured invocation so ChatPage auto-sends it
      // as a `slash_command` event (server resolves the skill) instead
      // of plain text the agent would see as a literal "/name". Native
      // terminal agents keep plain text — their CLI owns slash commands.
      setPendingInitialPrompt(data.id, {
        text: initialPrompt,
        skill: isNativeTerminalAgent
          ? null
          : matchSkillInvocation(initialPrompt, agent?.skills ?? []),
        files,
      });
      // Scope the recall entry to the new session id so ArrowUp surfaces it in
      // the freshly-opened chat (whose composer reads the same per-conversation
      // key). Sanitized text so recall reproduces exactly what was sent.
      appendPromptHistoryEntry(initialPrompt, data.id);
      // The session was created — drop any draft a detour back to this
      // screen stashed, so the next visit starts clean.
      landingDraft = null;
      // Only follow the create while the user is still on the landing
      // screen. A create that outlived it means they moved on to another
      // session; jumping them into this one now would hijack that. The
      // session is created either way and its first message stays held
      // for whenever they open it.
      if (onScreenRef.current) navigate(`/c/${data.id}`);
    } catch {
      returnDraftToUser();
      setCreateError("Couldn't reach the server. Check your connection and try again.");
    } finally {
      setCreating(false);
    }
  }

  const placeholderText = selectedProject
    ? `Start a new session in ${selectedProject}`
    : "Describe a task to start a new session…";

  // The working-directory chip — a single Popover trigger button that opens
  // the file browser. The directory-conflict warning lives inside the browser
  // (a banner on the occupied folder), not on the chip.
  const workspaceChip = (
    <button
      type="button"
      className="flex h-6 cursor-pointer items-center gap-1 rounded-full px-2.5 text-sm font-normal text-muted-foreground transition-colors hover:text-foreground"
      data-testid="new-chat-landing-workspace-chip"
    >
      <FolderIcon className="ui-icon" />
      {/* Label collapses to icon-only on narrow viewports (mobile). Capped
          tight so a long working-directory path truncates instead of pushing
          the chip row onto a second line. */}
      <span className="hidden max-w-40 truncate text-sm sm:block">{workspaceLabel}</span>
      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
    </button>
  );

  return (
    // pb-12 lifts the content slightly above the geometric center, where
    // the hero reads better optically.
    <div
      ref={setLandingSurface}
      className="flex flex-1 items-center justify-center"
      data-testid="new-chat-landing"
    >
      {/* Padding lives inside the 840px cap, so the composer renders at
          840 − 80 = 760px max on desktop. px-4 on phones (16px gutters)
          keeps the composer from feeling cramped against the viewport
          edges; widens to the full px-10 at the md breakpoint and up. */}
      <div className="flex w-full max-w-[840px] flex-col items-center gap-6 px-4 pt-8 pb-16 md:select-none md:px-10">
        <div className="flex w-full flex-col items-center justify-center gap-3.5">
          {selectedProject ? (
            // Landing inside a project: swap Otto's eyes for the same folder
            // icon the sidebar uses for a project, and name the project. Sized
            // to Otto's h-16 box so the centered composer doesn't shift when
            // toggling between the two landings.
            <span className="flex h-16 shrink-0 items-center">
              <div className="w-14 h-14 flex rounded-xl bg-tag-pink items-center justify-center">
                <FolderIcon className="size-6 text-brand-accent" />
              </div>
            </span>
          ) : (
            <OttoEyes className="h-14 w-auto shrink-0" />
          )}
          <h1 className="min-w-0 break-words text-center text-[1.5em] md:text-[2.15em] font-normal tracking-[-0.05em] text-foreground line-clamp-2 sm:text-left">
            {selectedProject || "What should we build?"}
          </h1>
        </div>
        <div className="relative flex w-full flex-col gap-1">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleCreate();
            }}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            // A home-specific focus shadow adds depth without a resting shadow
            // or focus border.
            // dark:bg-card-solid stays opaque so dark glass --card doesn't show
            // through. Drag-over keeps its separate inset ring.
            className={cn(
              "relative z-10 flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid transition-shadow duration-150 has-[textarea:focus]:shadow-[var(--composer-shadow-focus)]",
              isDragActive && "ring-2 ring-ring ring-inset",
            )}
            data-testid="new-chat-landing-composer"
          >
            {isDragActive && (
              <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
                <span className="text-ui font-medium text-ring">Drop files here</span>
              </div>
            )}
            {/* Skill suggestions — floats above the composer box. */}
            {slashMenuOpen && (
              <SlashCommandMenu
                query={slashMenuQuery}
                activeIndex={slashMenuIndex}
                onSelect={applySlashSelection}
                commands={skillCommands}
              />
            )}
            {/* "@"-file-mention browser — native terminal agents with a workspace */}
            {(mentionOpen || mentionListingPending) && (
              <FileMentionMenu
                currentDir={mentionDir}
                activeIndex={mentionIndex}
                entries={mentionEntries}
                loading={mentionListingPending}
                onOpenDir={openMentionDir}
                onAttach={attachMention}
              />
            )}
            <textarea
              ref={textareaRef}
              value={message}
              onChange={(e) => {
                setMessage(e.target.value);
                // Recompute the active "@"-mention from the caret each keystroke
                // (native terminal agents with a workspace — ``mentionEnabled``).
                setMention(
                  mentionEnabled
                    ? detectMentionAt(
                        e.target.value,
                        e.target.selectionStart ?? e.target.value.length,
                      )
                    : null,
                );
              }}
              onFocus={() => {
                // From here the textarea's caret is one the user placed, so
                // dictation inserts there instead of at the end of the draft.
                dictation.noteFocus();
              }}
              onBlur={() => {
                // Dismiss the mention menu when focus leaves the textarea; menu
                // rows preventDefault on mousedown so selecting one doesn't blur.
                dismissMention();
              }}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
              }}
              onKeyDown={(e) => {
                if (isImeCompositionKeyEvent(e, isComposingRef.current)) {
                  return;
                }

                // "@"-mention menu navigation (shared useMentionBrowser) —
                // mutually exclusive with the slash menu (a token can't be both)
                // and takes priority over submission.
                if (handleMentionKeyDown(e)) return;

                // While the skills menu is open, ArrowUp/Down navigate it and
                // Enter/Tab complete the highlighted item — these take
                // priority over submission (same UX as the in-session
                // composer).
                if (slashMenuOpen && slashMenuMatches.length > 0) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setSlashMenuIndex((i) => (i + 1) % slashMenuMatches.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setSlashMenuIndex((i) => (i <= 0 ? slashMenuMatches.length - 1 : i - 1));
                    return;
                  }
                  if (
                    (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) &&
                    slashMenuIndex >= 0
                  ) {
                    e.preventDefault();
                    applySlashSelection(slashMenuMatches[slashMenuIndex]!);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    // Dismiss the menu by clearing the draft so the user can
                    // start fresh.
                    setMessage("");
                    setSlashMenuIndex(-1);
                    return;
                  }
                }
                // Enter sends; Shift+Enter inserts a newline.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  // The mention menu is briefly closed while its listing loads;
                  // swallow Enter so the in-progress "@dir/" token isn't sent.
                  if (mentionListingPending) return;
                  void handleCreate();
                }
              }}
              onPaste={(e) => {
                // Pasted images/files attach instead of inserting as text,
                // mirroring the in-session composer.
                const pasted = Array.from(e.clipboardData.items)
                  .filter((item) => item.kind === "file")
                  .map((item) => item.getAsFile())
                  .filter((f): f is File => f !== null);
                if (pasted.length > 0) {
                  e.preventDefault();
                  addFiles(pasted);
                }
              }}
              // Suppress the native placeholder when the overlay supplies its
              // own prompt text; aria-label preserves the accessible name.
              placeholder={pillSkills.length > 0 ? "" : placeholderText}
              aria-label={placeholderText}
              rows={1}
              autoFocus
              data-testid="new-chat-landing-input"
              // Compose-pill text spec: SF Pro Text system stack at
              // 14px/20px. (Note: sub-16px inputs make mobile Safari
              // auto-zoom on focus — accepted tradeoff per the design.)
              // Heights are border-box (12px top + 8px bottom padding lives
              // inside them): max 200px = the spec's 180px of content.
              // A 60px floor holds two 20px lines plus that padding;
              // useAutoGrowTextarea expands from there to the unchanged cap.
              className="min-h-[60px] max-h-[200px] w-full resize-none overflow-y-auto bg-transparent px-4 pt-3 pb-2 font-['SF_Pro_Text',-apple-system,BlinkMacSystemFont,system-ui,sans-serif] text-ui leading-5 text-foreground outline-none placeholder:text-muted-foreground md:select-text"
            />
            {/* Gated on an empty draft so it reads as the placeholder.
                pointer-events-none lets clicks fall through to focus the
                textarea; the pills themselves opt back in. */}
            {pillSkills.length > 0 && message.length === 0 && (
              <div className="pointer-events-none absolute inset-x-4 top-3 flex flex-wrap items-center gap-2">
                <span className="font-['SF_Pro_Text',-apple-system,BlinkMacSystemFont,system-ui,sans-serif] text-ui leading-5 text-muted-foreground">
                  Describe a task, or try a skill
                </span>
                <SkillPills skills={pillSkills} onPick={applySkillPill} />
              </div>
            )}
            {/* Hidden file input for the attach button. */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/*,application/pdf,text/*,application/json"
              className="hidden"
              data-testid="new-chat-landing-file-input"
              onChange={(e) => {
                if (e.target.files) {
                  addFiles(Array.from(e.target.files));
                  // Reset so the same file can be re-selected.
                  e.target.value = "";
                }
              }}
            />
            {/* "@"-mention chips — one per tagged workspace file/folder. Each is
                delivered as an "[Attached: <path>]" marker prepended to the
                first message at create time. */}
            {mentionedItems.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-4 pb-2">
                {mentionedItems.map((item, i) => (
                  <span
                    key={mentionItemPath(item)}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
                  >
                    {item.isDir ? (
                      <FolderIcon className="size-3 shrink-0" />
                    ) : (
                      <FileTextIcon className="size-3 shrink-0" />
                    )}
                    <span className="max-w-[200px] truncate" title={mentionItemPath(item)}>
                      @{item.path}
                      {item.isDir ? "/" : ""}
                    </span>
                    <button
                      type="button"
                      onClick={() => removeMentionedItem(i)}
                      className="ml-0.5 rounded-full hover:text-foreground"
                      aria-label={`Remove ${item.path}`}
                    >
                      <XIcon className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {/* File chips — shown below the textarea when files are attached. */}
            {files.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-4 pb-2">
                {files.map((file, i) => (
                  <span
                    key={attachmentKey(file)}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
                  >
                    {file.type.startsWith("image/") ? (
                      <ImageIcon className="size-3 shrink-0" />
                    ) : (
                      <FileTextIcon className="size-3 shrink-0" />
                    )}
                    <span className="max-w-[140px] truncate">{file.name || "image.png"}</span>
                    <button
                      type="button"
                      onClick={() => removeFile(i)}
                      className="ml-0.5 rounded-full hover:text-foreground"
                      aria-label={`Remove ${file.name || "image.png"}`}
                    >
                      <XIcon className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {/* No own bg — the pill paints the surface. An explicit bg-card
                here would also catch the .dark .bg-card glass rule (border +
                shadow) and visually split the pill in half. */}
            <div
              className="flex items-center justify-between px-2 pb-2"
              data-testid="new-chat-landing-actions"
            >
              {/* Attach + dictate — left side, mirroring the in-session composer. */}
              <div className="flex items-center gap-0.5">
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-9 md:size-8"
                  disabled={creating}
                  onClick={() => fileInputRef.current?.click()}
                  title="Attach files"
                  data-testid="new-chat-landing-attach"
                >
                  <PaperclipIcon className="size-4" data-icon-size="16" />
                  <span className="sr-only">Attach files</span>
                </Button>
                <ComposerMicButton
                  enableHotkey
                  disabled={creating}
                  onVoiceStart={() => {
                    voiceSnapshotRef.current = message;
                  }}
                  onVoiceDiscard={() => setMessage(voiceSnapshotRef.current)}
                  onTranscript={dictation.appendFinal}
                  onInterim={dictation.replaceInterim}
                />
              </div>
              <div className="flex items-center gap-0.5 md:gap-2">
                <div className="flex items-center rounded-lg transition-colors has-[button:not(:disabled)]:hover:bg-muted dark:has-[button:not(:disabled)]:hover:bg-muted/50 has-aria-expanded:bg-muted dark:has-aria-expanded:bg-muted/50 [&>button]:bg-transparent!">
                  {/* Agent / harness picker — selects the agent or harness only.
                    Its run-config knobs (model / effort / permission mode for
                    Claude Code, approval mode for Codex/OpenCode, exec mode for
                    Cursor, brain-harness override for bundle agents) live in the
                    gear-icon config modal beside it. */}
                  <AgentHarnessPicker
                    agentEntries={agentEntries}
                    harnessEntries={harnessEntries}
                    effectiveAgentId={effectiveAgentId}
                    agentLabel={agentLabel}
                    hasAgents={agentList.length > 0}
                    host={harnessWarningHost}
                    onSelectAgent={handleSelectAgent}
                    pendingAgent={pendingAgentAllowedOnTarget ? pendingAgent : null}
                    pendingAgentId={PENDING_AGENT_ID}
                    onSelectPending={handleSelectPending}
                    onCreateCustomAgent={() => setCreateAgentOpen(true)}
                    sandboxSelected={sandboxSelected}
                    triggerTooltip={
                      smartRoutingHarnessSelected ? AUTO_HARNESS_DESCRIPTION : undefined
                    }
                    autoHarnessAvailable={smartRoutingHarnessAvailable}
                    autoHarnessActive={smartRoutingHarnessSelected}
                    onSelectAutoHarness={handleSelectSmartRoutingHarness}
                    // Match the gear's touch-target height so both halves fill
                    // the shared pill; pr-2 equals the gear icon's own centering
                    // inset (8px) so the divider sits evenly between them.
                    triggerClassName="h-9 pr-2 md:h-8"
                  />
                  {/* Gear — opens the selected agent's run-config modal, behind
                    a hairline divider. Both are hidden when the selected agent
                    has no knobs to configure, leaving a plain single-segment
                    pill. Hovering shows the current settings so they're readable
                    without opening the modal. */}
                  {selectedAgent && selectedAgentHasKnobs && (
                    <>
                      {/* The segments' own padding (trigger pr-2, gear icon
                        centering) supplies the gap on either side. */}
                      <span aria-hidden className="h-4 w-px shrink-0 bg-border" />
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              className="size-9 text-muted-foreground md:size-8"
                              disabled={creating}
                              onClick={() => setConfigOpen(true)}
                              data-testid="new-chat-landing-config-gear"
                            >
                              <SettingsIcon className="size-4" data-icon-size="16" />
                              <span className="sr-only">
                                Configure {selectedAgent.display_name}
                              </span>
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent
                            side="top"
                            className="flex-col items-start gap-0.5 px-3 py-2"
                            data-testid="new-chat-landing-config-gear-tooltip"
                          >
                            {configSummary.map((row) => (
                              <span key={row.label} className="text-muted-foreground">
                                {row.label}:{" "}
                                <span className="text-background dark:text-popover-foreground">
                                  {row.value}
                                </span>
                              </span>
                            ))}
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </>
                  )}
                </div>
                {selectedAgent && selectedAgentHasKnobs && (
                  <HarnessConfigModal
                    open={configOpen}
                    onOpenChange={setConfigOpen}
                    agent={selectedAgent}
                    brainHarnessLabels={brainHarnessLabels}
                    host={harnessWarningHost}
                    hideUnconfigured={hideUnconfiguredHarnesses}
                    smartRoutingEligible={smartRoutingEligible}
                    permissionMode={permissionMode}
                    approvalMode={approvalMode}
                    cursorExecMode={cursorExecMode}
                    bypassSandbox={bypassSandbox}
                    pickedModel={pickedModel}
                    claudeModelOptions={claudeModelOptions}
                    claudeModelsLoading={
                      !sandboxSelected && selectedHostId !== null && hostClaudeModelsLoading
                    }
                    codexModelOptions={codexModelOptions}
                    codexModelsLoading={
                      !sandboxSelected && selectedHostId !== null && hostCodexModelsLoading
                    }
                    pickedEffort={pickedEffort}
                    pickedHarness={pickedHarness}
                    costControlMode={costControlMode}
                    setPermissionMode={setPermissionMode}
                    setApprovalMode={setApprovalMode}
                    setCursorExecMode={setCursorExecMode}
                    setBypassSandbox={setBypassSandbox}
                    setPickedModel={setPickedModel}
                    setPickedEffort={setPickedEffort}
                    setPickedHarness={handleSetPickedHarness}
                    setCostControlMode={setCostControlMode}
                  />
                )}
                {/* Routing is not a standalone composer toggle — it folds into
                  the gear modal's Model dropdown as an "Smart Routing"
                  option (see HarnessConfigModal). */}
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="inline-flex">
                        <Button
                          type="submit"
                          size="icon"
                          disabled={!canSubmit}
                          aria-label={creating ? "Starting session" : "Start session"}
                          aria-busy={creating}
                          data-testid="new-chat-landing-submit"
                          className="size-8 rounded-lg bg-foreground disabled:bg-muted disabled:text-muted-foreground transition-opacity hover:opacity-80 disabled:opacity-100 "
                        >
                          {creating ? (
                            <Loader2Icon className="size-4 animate-spin" />
                          ) : (
                            <ArrowUpIcon className="size-4" viewBox="4 4 16 16" />
                          )}
                        </Button>
                      </span>
                    </TooltipTrigger>
                    {submitDisabledReason != null && (
                      <TooltipContent>{submitDisabledReason}</TooltipContent>
                    )}
                  </Tooltip>
                </TooltipProvider>
              </div>
            </div>
          </form>
          {/* Footer tray (host / cwd / worktree). Sits below the composer with
              symmetric vertical padding and no overlap. */}
          <div
            className="relative z-0 flex w-full items-center rounded-b-2xl py-1.5 pr-4 pl-2"
            data-testid="new-chat-landing-footer"
          >
            <div className="flex flex-wrap items-center gap-1">
              {/* Host chip */}
              <DropdownMenu
                onOpenChange={(open) => {
                  // Run a requested "connect this machine" only once the menu
                  // has closed.
                  if (!open && pendingConnectRef.current) {
                    pendingConnectRef.current = false;
                    void connectThisMachine();
                  }
                }}
              >
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className="flex h-6 cursor-pointer items-center gap-1 rounded-full px-2.5 text-sm font-normal text-muted-foreground transition-colors hover:text-foreground"
                    data-testid="new-chat-landing-host-chip"
                  >
                    {selectedHost?.status === "online" && !sandboxSelected ? (
                      <>
                        <span aria-hidden className="size-2 shrink-0 rounded-full bg-success" />
                        <span className="sr-only">Online</span>
                      </>
                    ) : isCloudHost ? (
                      <MonitorCloudIcon className="ui-icon" />
                    ) : (
                      <MonitorIcon className="ui-icon" />
                    )}
                    <span className="hidden max-w-32 truncate text-sm sm:block">{hostLabel}</span>
                    <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="min-w-52">
                  {/* Server-provisioned sandbox — only advertised when
                    /v1/info reports managed_sandboxes_enabled. Pinned
                    first, above the connected-host list. */}
                  {(managedSandboxesEnabled || showDisabledSandboxWithDocs) && (
                    <>
                      {managedSandboxesEnabled ? (
                        <DropdownMenuItem
                          onSelect={selectSandbox}
                          data-testid="new-chat-landing-sandbox-option"
                          data-active={sandboxSelected ? "true" : undefined}
                          className="text-sm data-[active=true]:bg-muted dark:data-[active=true]:bg-muted/50"
                        >
                          <span className="flex items-center gap-2">
                            <MonitorCloudIcon className="size-4 text-muted-foreground" />
                            <span className="text-sm">{sandboxLabel}</span>
                          </span>
                        </DropdownMenuItem>
                      ) : (
                        <DropdownMenuItem
                          aria-disabled="true"
                          onSelect={(e) => e.preventDefault()}
                          className="flex items-center justify-between px-2 py-1.5 text-sm text-muted-foreground opacity-60"
                          data-testid="new-chat-landing-sandbox-option-disabled"
                        >
                          <span className="flex items-center gap-2">
                            <MonitorCloudIcon className="size-4 text-muted-foreground" />
                            <span className="text-sm">New Sandbox</span>
                          </span>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <button
                                type="button"
                                className="inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground/80 hover:text-foreground"
                                aria-label="Why New Sandbox is unavailable"
                                onClick={(e) => e.stopPropagation()}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter" || e.key === " ") e.stopPropagation();
                                }}
                              >
                                <CircleHelpIcon className="size-3.5" />
                              </button>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-64">
                              {newSandboxTooltipContent}
                            </TooltipContent>
                          </Tooltip>
                        </DropdownMenuItem>
                      )}
                      <DropdownMenuSeparator />
                    </>
                  )}
                  {allHosts.length === 0 && !showConnectThisMachine && (
                    <div className="px-2 py-1.5 text-sm text-muted-foreground">
                      No hosts connected yet.
                    </div>
                  )}
                  {onlineHosts.map((host) => (
                    <DropdownMenuItem
                      key={host.host_id}
                      onSelect={() => selectHost(host.host_id)}
                      data-testid={`new-chat-landing-host-${host.host_id}`}
                      data-active={host.host_id === selectedHostId ? "true" : undefined}
                      className="text-sm data-[active=true]:bg-muted dark:data-[active=true]:bg-muted/50"
                    >
                      <HostOption
                        host={host}
                        displayName={displayNameForHost(
                          host,
                          thisMachineHostId,
                          navigator.userAgent,
                        )}
                      />
                    </DropdownMenuItem>
                  ))}
                  {offlineHosts.map((host) => {
                    // This machine, offline: make the row itself the connect
                    // affordance instead of a disabled entry + a duplicate "Run
                    // on this machine" item. Connect after the menu closes.
                    if (host.host_id === thisMachineHostId && canConnectThisMachine) {
                      return (
                        <DropdownMenuItem
                          key={host.host_id}
                          onSelect={() => {
                            pendingConnectRef.current = true;
                          }}
                          disabled={connectingThisMachine}
                          data-testid="new-chat-landing-run-on-this-machine"
                          className="text-sm"
                        >
                          <HostOption
                            host={host}
                            displayName={displayNameForHost(
                              host,
                              thisMachineHostId,
                              navigator.userAgent,
                            )}
                            subtitle={connectingThisMachine ? "connecting…" : "select to connect"}
                          />
                        </DropdownMenuItem>
                      );
                    }
                    return (
                      <DropdownMenuItem key={host.host_id} disabled className="text-sm">
                        <HostOption
                          host={host}
                          displayName={displayNameForHost(
                            host,
                            thisMachineHostId,
                            navigator.userAgent,
                          )}
                        />
                      </DropdownMenuItem>
                    );
                  })}
                  {/* Desktop shell, machine not in the list yet: offer to connect
                    it in one click. */}
                  {showConnectThisMachine && (
                    <DropdownMenuItem
                      onSelect={() => {
                        pendingConnectRef.current = true;
                      }}
                      disabled={connectingThisMachine}
                      data-testid="new-chat-landing-run-on-this-machine"
                      className="gap-2 text-sm"
                    >
                      <MonitorIcon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="text-sm">
                        {connectingThisMachine ? "Connecting this machine…" : "Run on this machine"}
                      </span>
                    </DropdownMenuItem>
                  )}
                  {(allHosts.length > 0 || showConnectThisMachine) && <DropdownMenuSeparator />}
                  {/* Persistent escape hatch: open the connect-a-host
                    instructions. Present even with zero hosts so a fresh user
                    is never stuck. */}
                  <DropdownMenuItem
                    onSelect={() => setConnectOpen(true)}
                    data-testid="new-chat-landing-connect-host"
                    className="gap-2 text-ui text-muted-foreground"
                  >
                    <PlusIcon className="size-3.5" />
                    Connect new host
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>

              {/* Sandbox repository chip — the sandbox counterpart of the
                working-directory chip. There is no filesystem to browse
                before the sandbox exists, so the workspace is specified as
                a git repository URL (+ optional branch) the server clones
                at create time. Blank = empty server-created workspace. */}
              {sandboxSelected && (
                <Popover>
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      className="flex h-6 cursor-pointer items-center gap-1 rounded-full px-2.5 text-sm font-normal text-muted-foreground transition-colors hover:text-foreground"
                      data-testid="new-chat-landing-repo-chip"
                    >
                      <GitBranchIcon className="ui-icon" />
                      <span className="hidden max-w-40 truncate text-sm sm:block">
                        {sandboxRepoLabel}
                      </span>
                      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
                    </button>
                  </PopoverTrigger>
                  <PopoverContent align="start" className="w-96 p-3">
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center gap-1.5">
                        <label
                          htmlFor="landing-repo-url"
                          className="text-sm font-medium text-foreground"
                        >
                          Repository (optional)
                        </label>
                        {databricksGitCredentialsTooltipContent && (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <button
                                type="button"
                                className="inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground"
                                aria-label="How to set up Databricks git credentials"
                              >
                                <CircleHelpIcon className="size-3.5" />
                              </button>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-64">
                              {databricksGitCredentialsTooltipContent}
                            </TooltipContent>
                          </Tooltip>
                        )}
                      </div>
                      <input
                        id="landing-repo-url"
                        type="text"
                        value={sandboxRepoUrl}
                        onChange={(e) => setSandboxRepoUrl(e.target.value)}
                        placeholder="https://github.com/org/repo"
                        className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-repo-input"
                      />
                      <input
                        type="text"
                        value={sandboxRepoBranch}
                        onChange={(e) => setSandboxRepoBranch(e.target.value)}
                        placeholder="Branch (defaults to the repo's default)"
                        aria-label="Repository branch"
                        className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-repo-branch-input"
                      />
                      <p className="text-sm text-muted-foreground">
                        Cloned into the sandbox as the session's working directory. Leave blank to
                        start in an empty workspace.
                      </p>
                    </div>
                  </PopoverContent>
                </Popover>
              )}

              {/* Working directory chip — opens the file browser directly (no
                separate "browse" toggle). onNavigate updates the workspace
                live as the user browses (no "Select" button); the popover
                closes on click-out. The directory-conflict warning shows as a
                banner inside the browser on the occupied folder. Hidden for
                sandbox sessions — the repository chip above replaces it (the
                server creates the directory inside the sandbox). */}
              {!sandboxSelected && (
                <Popover open={workspacePopoverOpen} onOpenChange={setWorkspacePopoverOpen}>
                  <PopoverTrigger asChild>{workspaceChip}</PopoverTrigger>
                  {/* Cap to the viewport so the 420px browser can't overflow a
                  narrow screen; desktop still gets the full width. */}
                  <PopoverContent align="start" className="w-[min(420px,calc(100vw-2rem))] p-0">
                    {selectedHostId ? (
                      <WorkspacePicker
                        hostId={selectedHostId}
                        initialPath={
                          isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined
                        }
                        onNavigate={setWorkspace}
                        // Warn when browsing into a directory other live agents
                        // occupy. Suppressed only when a NEW isolated worktree
                        // will be created (no shared-dir conflict then). When
                        // starting directly in an existing worktree the branch
                        // is prefilled but the dir IS shared, so keep warning.
                        occupancyForPath={
                          !shouldCreateWorktree
                            ? (abs) => occupancyByDir.get(normalizeWorkspacePath(abs) ?? "") ?? 0
                            : undefined
                        }
                      />
                    ) : (
                      <p className="p-3 text-sm text-muted-foreground">Select a host first.</p>
                    )}
                  </PopoverContent>
                </Popover>
              )}

              {/* Git worktree chip — hidden for sandbox sessions (worktree
                creation requires a caller-supplied host_id). */}
              {!sandboxSelected && (
                <Popover open={worktreePopoverOpen} onOpenChange={setWorktreePopoverOpen}>
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      className="flex h-6 cursor-pointer items-center gap-1 rounded-full px-2.5 text-sm font-normal text-muted-foreground transition-colors hover:text-foreground"
                      data-testid="new-chat-landing-branch-chip"
                    >
                      <GitBranchIcon className="ui-icon" />
                      <span className="hidden max-w-32 truncate text-sm sm:block">
                        {worktreeLabel}
                      </span>
                      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
                    </button>
                  </PopoverTrigger>
                  <PopoverContent
                    align="start"
                    collisionPadding={16}
                    // No overflow clip here — the worktree dropdown floats as an
                    // absolute overlay (below) and must be able to escape the
                    // popover's padding box.
                    className="w-[min(20rem,calc(100vw-2rem))] p-3"
                  >
                    <div className="flex flex-col gap-2">
                      <label
                        htmlFor="landing-branch-name"
                        className="text-sm font-medium text-foreground"
                      >
                        Git worktree branch (optional)
                      </label>
                      {/* Help text sits above the field. The warning for a picked
                        existing worktree stays below the input (contextual to the
                        selection). */}
                      <p className="text-sm text-muted-foreground">
                        New branch name, or pick an existing worktree. Leave blank to start directly
                        in the working directory.
                      </p>
                      {/* The branch field is a combobox: focusing it reveals the
                        repo's existing worktrees, and typing filters them.
                        Picking one starts in that worktree; a name matching none
                        creates a new worktree. */}
                      <div className="relative flex flex-col">
                        <input
                          id="landing-branch-name"
                          type="text"
                          value={branchName}
                          onChange={(e) => setBranchName(e.target.value)}
                          onFocus={() => setBranchInputFocused(true)}
                          // Delay so a click on a dropdown option registers
                          // before the list unmounts on blur.
                          onBlur={() => setTimeout(() => setBranchInputFocused(false), 120)}
                          placeholder="feature/my-branch"
                          role="combobox"
                          aria-expanded={branchInputFocused && filteredWorktrees.length > 0}
                          aria-autocomplete="list"
                          // Suppress the browser's native autofill dropdown so it
                          // doesn't overlay our worktree combobox. `off` alone is
                          // ignored by some browsers, so also disable spellcheck /
                          // autocorrect and give it an unrecognized name.
                          autoComplete="off"
                          autoCorrect="off"
                          autoCapitalize="off"
                          spellCheck={false}
                          name="omnigent-worktree-branch"
                          // pr-9 leaves room for the generate button overlaid at
                          // the right edge.
                          className="rounded-md border border-input bg-background py-2 pr-9 pl-3 text-sm outline-none transition-colors focus-visible:border-ring"
                          data-testid="new-chat-landing-branch-input"
                        />
                        {/* Fill a unique branch name for a throwaway worktree.
                          onMouseDown so it fires before the input's blur closes
                          the combobox and preventDefault keeps focus on the
                          input. */}
                        <button
                          type="button"
                          onMouseDown={(e) => {
                            e.preventDefault();
                            generateBranchName();
                          }}
                          title="Generate a unique branch name"
                          aria-label="Generate a unique branch name"
                          className="absolute top-0 right-0 flex h-9 w-9 items-center justify-center text-muted-foreground transition-colors hover:text-foreground"
                          data-testid="new-chat-landing-branch-generate"
                        >
                          <ShuffleIcon className="size-4" />
                        </button>
                        {branchInputFocused && filteredWorktrees.length > 0 && (
                          <div
                            // Floats over the popover as a combobox popup, so it
                            // doesn't stretch the box. Bounded height + internal
                            // scroll keep it from running off the viewport.
                            className="absolute top-full right-0 left-0 z-20 mt-1 flex max-h-40 flex-col overflow-y-auto rounded-[12px] border border-border bg-popover p-2 shadow-menu"
                            data-testid="new-chat-landing-worktree-dropdown"
                          >
                            <span className="px-1.5 py-1 text-sm font-medium text-muted-foreground">
                              Existing worktrees
                            </span>
                            <ul className="flex flex-col gap-0.5">
                              {filteredWorktrees.map((w) => {
                                const selected =
                                  normalizeWorkspacePath(w.path) ===
                                  normalizeWorkspacePath(workspaceTrimmed);
                                return (
                                  <li key={w.path}>
                                    <button
                                      type="button"
                                      // onMouseDown (not onClick): fires before the
                                      // input's blur, so the selection lands even
                                      // though blur is about to hide the list.
                                      onMouseDown={(e) => {
                                        e.preventDefault();
                                        setWorkspace(w.path);
                                        setBranchInputFocused(false);
                                        setWorktreePopoverOpen(false);
                                      }}
                                      className={`flex w-full flex-col items-start gap-0.5 rounded-md px-1.5 py-1 text-left text-sm transition-colors hover:bg-muted dark:hover:bg-muted/50 ${
                                        selected ? "bg-muted dark:bg-muted/50" : ""
                                      }`}
                                      data-testid="new-chat-landing-worktree-option"
                                    >
                                      <span className="font-medium text-foreground">
                                        {w.branch ?? "(detached)"}
                                      </span>
                                      {/* Tail-truncated so the disambiguating
                                      folder shows, not a shared prefix; full
                                      path on hover. */}
                                      <span
                                        className="w-full truncate text-muted-foreground"
                                        title={w.path}
                                      >
                                        {worktreePathTail(w.path)}
                                      </span>
                                    </button>
                                  </li>
                                );
                              })}
                            </ul>
                          </div>
                        )}
                      </div>
                      {/* Base branch only matters when creating a NEW worktree
                        — hidden once the workspace points at an existing one
                        (no worktree is created, so there's nothing to base). */}
                      {branchName.trim() !== "" && !startInExistingWorktree && (
                        <input
                          type="text"
                          value={baseBranch}
                          onChange={(e) => setBaseBranch(e.target.value)}
                          placeholder="Base branch (defaults to current)"
                          aria-label="Base branch"
                          className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
                          data-testid="new-chat-landing-base-branch-input"
                        />
                      )}
                      {startInExistingWorktree && (
                        <p
                          className="text-sm text-amber-600 dark:text-amber-500"
                          data-testid="new-chat-landing-existing-worktree-warning"
                        >
                          Starts in existing worktree, edit the name to create a new one.
                        </p>
                      )}
                    </div>
                  </PopoverContent>
                </Popover>
              )}

              {/* The session's project membership (from a `?project=` landing)
                is shown in the hero heading instead of a tray chip; filing on
                create still uses `selectedProject`. */}
            </div>
            {/* The agent / harness picker moved out of the tray and into the
                composer's right action cluster (next to Send) — see
                AgentHarnessPicker above. The tray now holds only the
                host / working-directory / worktree / project chips. */}
          </div>

          {/* Warn (don't block) when the selected agent's harness isn't
              configured on the selected host — the host re-checks at
              launch, so submitting surfaces a specific error if it
              really can't run. Normal-flow directly under the composer
              (like the createError line below) so it reads as part of it. */}
          {selectedAgentUnconfigured && (
            <HarnessSetupNotice
              agentName={selectedAgent?.display_name}
              hostName={harnessWarningHost?.name}
              harness={selectedAgent?.harness ?? null}
              reason={harnessUnavailableReasonOnHost(selectedAgent?.harness, harnessWarningHost)}
              featureEnabled={harnessInstallEnabled}
              onSetup={() =>
                setSetupTarget({
                  agentName: selectedAgent?.display_name,
                  harness: selectedAgent?.harness ?? null,
                  host: harnessWarningHost,
                })
              }
            />
          )}

          {/* Same slot, same styling as the readiness notice above: the host
              switch took Smart Routing away, so say so instead of quietly
              leaving a different agent selected. Suppressed while the
              readiness notice is up — one slot, and "set up this harness" is
              the more actionable of the two. */}
          {smartRoutingDropped && !selectedAgentUnconfigured && (
            <p
              className="flex items-center gap-2 pl-2 text-xs text-amber-600 dark:text-amber-500"
              data-testid="new-chat-landing-smart-routing-dropped"
            >
              <TriangleAlertIcon className="size-3.5 shrink-0" />
              <span>
                {smartRoutingDroppedMessage(smartRoutingDropped, {
                  hostName: harnessWarningHost?.name,
                  fallbackAgentName: selectedAgent?.display_name,
                })}
              </span>
            </p>
          )}

          {createError && (
            <p className="text-sm text-destructive" data-testid="new-chat-landing-error">
              {createError}
            </p>
          )}
        </div>
      </div>

      {/* Connect-host instructions, reachable from the host dropdown even when
          no hosts are online — the zero-host escape hatch. */}
      <Dialog open={connectOpen} onOpenChange={setConnectOpen}>
        <DialogContent className="sm:max-w-lg" data-testid="connect-host-dialog">
          <DialogHeader>
            <DialogTitle>Connect a host</DialogTitle>
          </DialogHeader>
          <ConnectHostInstructions
            serverUrl={serverUrl}
            label="Run this on the machine you want to use, then pick it from the host menu:"
          />
        </DialogContent>
      </Dialog>

      {/* Harness "Set up" dialog — the single home for install/login (and later
          API key / gateway) setup, opened from the composer notice or a picker
          row's "Set up →". */}
      <HarnessSetupDialog
        open={setupTarget !== null}
        onOpenChange={(open) => {
          if (!open) setSetupTarget(null);
        }}
        agentName={setupTarget?.agentName}
        harness={setupTarget?.harness ?? null}
        host={setupTarget?.host}
      />

      {/* Create custom agent dialog — opened from the agent picker dropdown. */}
      <CreateAgentDialog
        open={createAgentOpen}
        onOpenChange={setCreateAgentOpen}
        onCreate={(input) => {
          setPendingAgent(input);
          setPickedAgentId(PENDING_AGENT_ID);
          setPickedHarness(null);
        }}
      />
    </div>
  );
}
