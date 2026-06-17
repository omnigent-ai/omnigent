import { type DragEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  MonitorIcon,
  MonitorCloudIcon,
  CircleHelpIcon,
  ChevronDownIcon,
  GitBranchIcon,
  ArrowUpIcon,
  FileTextIcon,
  FolderIcon,
  ImageIcon,
  PaperclipIcon,
  PlusIcon,
  SettingsIcon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { authenticatedFetch } from "@/lib/identity";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { isSlashCommandText, SlashCommandMenu } from "@/components/SlashCommandMenu";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { appendPromptHistoryEntry } from "@/hooks/usePromptHistory";
import { CliCommandBlock } from "./CliCommandBlock";
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import { getCliServerUrl } from "@/lib/host";
import { getOmnigentHostConfig } from "@/lib/host";
import { readLastAgentId, writeLastAgentId } from "@/lib/agentPreferences";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { cn } from "@/lib/utils";
import {
  isNativeCodingAgent,
  nativeAgentHasCapability,
  nativeAgentSortRank,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import type { Conversation } from "@/hooks/useConversations";
import { OttoEyes } from "@/components/OttoEyes";
import { SkillPills } from "@/components/SkillPills";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { IntelligentModelControl, type CostControlMode } from "@/components/CostRoutingControl";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentRowTooltip } from "@/components/AgentHoverCard";

// Preferred display order for the built-in agent picker. The server
// returns agents newest-registered first (agent_store.list sorts by
// created_at desc), so pin the order users expect; any agent not listed
// here falls after, in server order.
const AGENT_DISPLAY_ORDER = ["Claude Code", "Codex", "Pi", "Cursor", "Polly", "Debby"];

// Built-in agents (by name slug) — the long-lived agents the server
// ships out of the box. The picker groups these first, then a divider,
// then custom (user-registered) agents. GET /v1/agents doesn't yet
// distinguish the two, so this is a frontend allowlist for now.
const BUILTIN_AGENTS = new Set([
  "claude-native-ui", // Claude Code
  "codex-native-ui", // Codex
  "pi-native-ui", // Pi
  "cursor-native-ui", // Cursor
  "polly",
  "debby",
]);

// Hidden on the new-session picker only (superseded by polly; older
// deployments still carry a seeded nessie row this filter keeps out).
const NEW_SESSION_HIDDEN_AGENTS = new Set(["nessie"]);

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

function HostOption({ host }: { host: Host }) {
  const isOnline = host.status === "online";
  return (
    <span className="flex items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 text-muted-foreground" />
      )}
      <span className="text-xs">{host.name}</span>
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${isOnline ? "text-green-600" : "text-muted-foreground"}`}
      >
        <span
          className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
        />
        {host.status}
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
      {label && <p className="text-xs text-muted-foreground">{label}</p>}
      {databricksFeatures ? (
        <Tabs defaultValue="local">
          <TabsList className="w-full">
            <TabsTrigger value="local" className="text-xs">
              Local machine
            </TabsTrigger>
            <TabsTrigger value="lakebox" className="text-xs">
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
 * Whether an agent's harness is known to be unconfigured on a host.
 *
 * Warning-only signal for the agent picker: `true` only when the host
 * explicitly reported the harness as not ready (CLI missing or no
 * default credential — see `omnigent setup`). A missing readiness map
 * (older host build) or an unknown harness yields `false`, so unknown
 * never warns; the host re-checks authoritatively at launch time.
 *
 * @param harness The agent's harness id as returned by `/v1/agents`,
 *   e.g. `"claude-sdk"` or `"codex"`. `null` when the agent has none.
 * @param host The selected host, or `undefined`/`null` when no
 *   connected host is selected (e.g. sandbox).
 * @returns `true` when the host explicitly reports the harness as
 *   unconfigured.
 */
export function harnessUnconfiguredOnHost(
  harness: string | null | undefined,
  host: Host | undefined | null,
): boolean {
  if (!harness || !host?.configured_harnesses) return false;
  return host.configured_harnesses[harness] === false;
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
  skills: ReadonlyArray<{ name: string }>,
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
/**
 * The permission-mode radio rows + previewed-description footer,
 * rendered inside the Advanced settings menu in the composer footer.
 *
 * The hovered/focused mode (whose description shows in the footer) is
 * local state: hovering rows re-renders only this component, not the
 * whole landing screen, and the menu unmounting on close resets the
 * preview so the next open shows the selected mode's blurb.
 *
 * @param value Currently selected mode, e.g. ``"default"``.
 * @param onValueChange Selection callback (receives the mode value).
 */
function PermissionModeOptions({
  value,
  onValueChange,
}: {
  value: string;
  onValueChange: (mode: string) => void;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = CLAUDE_NATIVE_PERMISSION_MODES.find(
    (m) => m.value === (previewed ?? value),
  )?.description;
  return (
    <>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {CLAUDE_NATIVE_PERMISSION_MODES.map((mode) => (
          <DropdownMenuRadioItem
            key={mode.value}
            value={mode.value}
            data-testid={`new-chat-landing-permission-${mode.value}`}
            onFocus={() => setPreviewed(mode.value)}
            onPointerEnter={() => setPreviewed(mode.value)}
            // pl only — the kit's pr-8 reserves room for the
            // absolutely-positioned check.
            // text-xs matches the other footer-tray menus (host picker).
            className="rounded-sm pl-2 py-1 text-xs"
          >
            {mode.label}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <p
        data-testid="new-chat-landing-permission-detail"
        // One reserved line, not two: reserving the longest blurb's wrapped
        // second line left a permanent blank row under one-line blurbs.
        className="min-h-5 px-2 pt-0.5 pb-1 text-xs leading-relaxed text-muted-foreground"
      >
        {detail}
      </p>
    </>
  );
}

/**
 * Codex approval-mode radio rows, rendered inside the Advanced settings
 * menu in the composer footer. Mirror of {@link PermissionModeOptions}
 * for the Codex-native agent.
 *
 * @param value Currently selected mode, e.g. ``"suggest"``.
 * @param onValueChange Selection callback (receives the mode value).
 */
function ApprovalModeOptions({
  value,
  onValueChange,
}: {
  value: string;
  onValueChange: (mode: string) => void;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = CODEX_NATIVE_APPROVAL_MODES.find(
    (m) => m.value === (previewed ?? value),
  )?.description;
  return (
    <>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {CODEX_NATIVE_APPROVAL_MODES.map((mode) => (
          <DropdownMenuRadioItem
            key={mode.value}
            value={mode.value}
            data-testid={`new-chat-landing-approval-${mode.value}`}
            onFocus={() => setPreviewed(mode.value)}
            onPointerEnter={() => setPreviewed(mode.value)}
            className="rounded-sm pl-2 py-1 text-xs"
          >
            {mode.label}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <p
        data-testid="new-chat-landing-approval-detail"
        className="min-h-5 px-2 pt-0.5 pb-1 text-xs leading-relaxed text-muted-foreground"
      >
        {detail}
      </p>
    </>
  );
}

/**
 * Brain-harness radio rows for an overridable bundle agent, rendered
 * inside the Advanced settings menu in the composer footer.
 *
 * @param value Effective harness id for the agent, e.g. ``"claude-sdk"``.
 * @param onValueChange Selection callback (receives the harness id).
 * @param host Host whose `configured_harnesses` drives per-row "needs
 *   setup" badges; undefined hides the badges (sandbox selected).
 */
function BrainHarnessOptions({
  value,
  onValueChange,
  host,
}: {
  value: string;
  onValueChange: (harness: string) => void;
  host: Host | undefined | null;
}) {
  return (
    <>
      <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
        Agent Harness
      </div>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {Object.entries(BRAIN_HARNESS_LABELS).map(([id, label]) => (
          <DropdownMenuRadioItem
            key={id}
            value={id}
            data-testid={`new-chat-landing-harness-${id}`}
            // pl only — the kit's pr-8 reserves room for the
            // absolutely-positioned check.
            // text-xs matches the other footer-tray menus (host picker).
            className="rounded-sm pl-2 py-1 text-xs"
          >
            <span className="flex-1">{label}</span>
            {harnessUnconfiguredOnHost(id, host) && (
              <Badge
                variant="outline"
                className="border-amber-300 bg-amber-50 text-[11px] text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
                data-testid={`new-chat-landing-harness-warning-${id}`}
              >
                needs setup
              </Badge>
            )}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
    </>
  );
}

export function NewChatLandingScreen() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const serverUrl = getCliServerUrl();
  const { data: agents } = useAvailableAgents();
  const { data: hosts } = useHosts();
  // Sessions the caller can access, to warn when a new session would share a
  // working directory with a live one (see the conflict tooltip below).
  const { data: directorySessions } = useDirectorySessions(true);

  const agentList = useMemo(() => {
    const displayRank = (name: string) => {
      const i = AGENT_DISPLAY_ORDER.indexOf(name);
      return i === -1 ? AGENT_DISPLAY_ORDER.length : i;
    };
    return [...(agents ?? [])]
      .filter((a) => !NEW_SESSION_HIDDEN_AGENTS.has(a.name))
      .sort(
        (a, b) =>
          nativeAgentSortRank(a) - nativeAgentSortRank(b) ||
          displayRank(a.display_name) - displayRank(b.display_name),
      );
  }, [agents]);

  // Split the picker into built-in agents (shipped out of the box) and
  // custom (user-registered) agents so the menu can group them with a
  // divider between, mirroring the permission-mode separator below.
  const builtinAgents = useMemo(
    () => agentList.filter((a) => BUILTIN_AGENTS.has(a.name)),
    [agentList],
  );
  const customAgents = useMemo(
    () => agentList.filter((a) => !BUILTIN_AGENTS.has(a.name)),
    [agentList],
  );

  const [message, setMessage] = useState<string>("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  // maxRows 9 = 180px of 20px lines, matching the composer's 200px
  // border-box max (180px content + 16px top / 4px bottom padding).
  useAutoGrowTextarea(textareaRef, message, 9);

  // Attachments for the first message — same affordances as the in-session
  // composer (paperclip + paste); carried to ChatPage via the pending
  // initial prompt and sent with the auto-dispatched first turn.
  const [files, setFiles] = useState<File[]>([]);
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

  // Seeded from the persisted last pick so a returning user starts on the
  // agent they used last; validated against the live list in
  // effectiveAgentId below (a stale id falls back to the default).
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(() => readLastAgentId());
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  // True when the user picked the sandbox option instead of a connected
  // host — the server provisions a sandbox host at create time
  // (host_type: "managed"), so no host_id or workspace is sent.
  const [sandboxSelected, setSandboxSelected] = useState(false);
  // Sandbox repository inputs — composed into the managed create's
  // `workspace` string (`<url>[#<branch>]`); both blank = empty
  // server-created workspace.
  const [sandboxRepoUrl, setSandboxRepoUrl] = useState<string>("");
  const [sandboxRepoBranch, setSandboxRepoBranch] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  const [branchName, setBranchName] = useState<string>("");
  const [baseBranch, setBaseBranch] = useState<string>("");
  // Permission mode for Claude Code (claude --permission-mode). Only
  // meaningful for the claude-native wrapper; ignored otherwise. Lives in
  // the footer tray's Advanced settings menu.
  const [permissionMode, setPermissionMode] = useState<string>(
    CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  );
  // Approval mode for Codex (codex --approval-mode). Only meaningful for
  // the codex-native wrapper; ignored otherwise. Lives in the footer
  // tray's Advanced settings menu.
  const [approvalMode, setApprovalMode] = useState<string>(CODEX_NATIVE_DEFAULT_APPROVAL_MODE);
  // Per-session brain-harness override for bundle agents (polly / debby).
  // null = the agent spec's declared harness (no override sent); cleared on
  // every agent switch so a pick never leaks across agents.
  const [pickedHarness, setPickedHarness] = useState<string | null>(null);
  // Per-session cost-control switch ("Cost Optimized" pill). Unset
  // (null) defers to the agent spec's default and is omitted from
  // the create body.
  const [costControlMode, setCostControlMode] = useState<CostControlMode>(null);
  // Controls the working-directory popover so picking a directory closes it.
  const [workspacePopoverOpen, setWorkspacePopoverOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // "Connect a host" instructions modal, opened from the host dropdown.
  const [connectOpen, setConnectOpen] = useState(false);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  const allHosts = hosts ?? [];
  const onlineHosts = allHosts.filter((h) => h.status === "online");
  const offlineHosts = allHosts.filter((h) => h.status === "offline");

  // Auto-select the FIRST AVAILABLE option, mirroring the menu order, so
  // a session can be started without an explicit pick: the sandbox when
  // the server supports it (it's pinned first in the picker), else the
  // first online host. Only fills an empty slot; explicit choices are
  // never overridden.
  useEffect(() => {
    if (sandboxSelected) return;
    if (selectedHostId !== null) return;
    if (managedSandboxesEnabled) {
      setSandboxSelected(true);
      return;
    }
    const firstOnline = (hosts ?? []).find((h) => h.status === "online");
    if (firstOnline) setSelectedHostId(firstOnline.host_id);
  }, [hosts, selectedHostId, sandboxSelected, managedSandboxesEnabled]);

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

  // Seed the working directory once per host, into an empty field only, so an
  // explicit pick isn't clobbered. Prefer the most-recent path; else the
  // derived home (which can arrive a render later, hence the dep).
  const seededHostRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedHostId === null) return;
    if (seededHostRef.current === selectedHostId) return;
    const candidate = recent[0] ?? derivedHome;
    if (!candidate) return;
    seededHostRef.current = selectedHostId;
    setWorkspace((cur) => (cur === "" ? candidate : cur));
  }, [selectedHostId, recent, derivedHome]);

  // A pick only wins while it exists in the list — a persisted id whose
  // agent has since been unregistered (or hidden) falls back to the default.
  const effectiveAgentId =
    (agentList.some((a) => a.id === pickedAgentId) ? pickedAgentId : agentList[0]?.id) ?? null;
  const selectedAgent = agentList.find((a) => a.id === effectiveAgentId);
  const supportsPermissionMode = nativeAgentHasCapability(selectedAgent, "permissionMode");
  const supportsApprovalMode = nativeAgentHasCapability(selectedAgent, "approvalMode");
  // Native-terminal agents interpret slash commands inside their own CLI
  // (the runner injects the text verbatim), so the landing composer must
  // not intercept them — no skills menu, no slash_command routing.
  const isNativeTerminalAgent = isNativeCodingAgent(selectedAgent);
  const selectedHost = allHosts.find((h) => h.host_id === selectedHostId);
  // Warn-only readiness signal for the agent picker: only meaningful when
  // a connected host is selected (a sandbox provisions its own tooling).
  // Selection stays allowed — the host re-checks at launch and the create
  // call surfaces a specific error if the harness really can't run.
  const harnessWarningHost = !sandboxSelected ? selectedHost : undefined;
  const selectedAgentUnconfigured = harnessUnconfiguredOnHost(
    selectedAgent?.harness,
    harnessWarningHost,
  );
  const workspaceTrimmed = workspace.trim();
  const workspaceValid = isValidWorkspace(workspace);
  const isCloudHost =
    sandboxSelected || (selectedHost?.name?.toLowerCase().includes("cloud") ?? false);

  // Sessions on the selected host that have a workspace — candidates for a
  // directory conflict, fed to the runner-health poll so only *connected*
  // agents count (same /health signal as the sidebar dots).
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
    ? Object.keys(skillCommands).filter((name) =>
        name.slice(1).startsWith(slashMenuQuery.toLowerCase()),
      )
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
  const hostLabel = sandboxSelected
    ? sandboxLabel
    : (selectedHost?.name ?? (onlineHosts.length === 0 ? "No hosts" : "Select host"));
  const worktreeLabel = branchName.trim() || "No worktree";
  // Sandbox repository chip label: repo name (server's clone-dir rule)
  // plus the pinned branch, e.g. "repo#main"; placeholder when unset.
  const sandboxRepoName = deriveRepoName(sandboxRepoUrl);
  const sandboxRepoLabel = sandboxRepoName
    ? sandboxRepoBranch.trim()
      ? `${sandboxRepoName}#${sandboxRepoBranch.trim()}`
      : sandboxRepoName
    : "Repository";
  // Selected permission mode's display label — appended to the agent picker
  // label (non-default picks only) so a changed mode stays visible while the
  // radios live in the footer tray's Advanced settings menu.
  const permissionModeLabel =
    CLAUDE_NATIVE_PERMISSION_MODES.find((m) => m.value === permissionMode)?.label ?? permissionMode;
  const approvalModeLabel =
    CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.label ?? approvalMode;
  // Effective brain harness for the selected agent: the user's pick, else
  // the spec's declared harness. null for non-overridable agents (native
  // wrappers, agents whose spec failed to load).
  const selectedAgentDefaultHarness =
    selectedAgent?.harness != null && selectedAgent.harness in BRAIN_HARNESS_LABELS
      ? selectedAgent.harness
      : null;
  // The label suffixes the permission/approval mode / harness only when the
  // user explicitly changed it in the Advanced menu — defaults read as just
  // the agent name. pickedHarness is non-null only for an explicit
  // non-default pick (re-picking the spec default clears it).
  const agentLabel = selectedAgent
    ? supportsPermissionMode && permissionMode !== CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
      ? `${selectedAgent.display_name} (${permissionModeLabel})`
      : supportsApprovalMode && approvalMode !== CODEX_NATIVE_DEFAULT_APPROVAL_MODE
        ? `${selectedAgent.display_name} (${approvalModeLabel})`
        : pickedHarness != null
          ? `${selectedAgent.display_name} (${BRAIN_HARNESS_LABELS[pickedHarness] ?? pickedHarness})`
          : selectedAgent.display_name
    : "Select agent";

  /**
   * Render one agent row in the picker dropdown.
   *
   * The short blurb (from AGENT_PICKER_DESCRIPTIONS, hardcoded for a
   * few agents) renders NEXT TO the name in lighter text, and only
   * when one exists — agents without a blurb show just their name in
   * the menu. The full spec description is never shown inline; it
   * surfaces on hover via AgentRowTooltip, and the closed-state button
   * label (agentLabel) shows only the name.
   */
  const renderAgentRow = (agent: AvailableAgent) => {
    const blurb = AGENT_PICKER_DESCRIPTIONS[agent.name];
    return (
      <DropdownMenuItem
        key={agent.id}
        data-testid={`new-chat-landing-agent-${agent.id}`}
        data-active={agent.id === effectiveAgentId ? "true" : undefined}
        onSelect={() => {
          // Switching agents drops the harness override so a
          // pick never leaks across agents.
          if (agent.id !== effectiveAgentId) setPickedHarness(null);
          setPickedAgentId(agent.id);
          // Explicit picks persist; auto-defaults never do.
          writeLastAgentId(agent.id);
        }}
        className="items-start gap-2 rounded-sm px-2 py-1.5 text-sm data-[active=true]:bg-accent/60 data-[active=true]:text-foreground"
      >
        {/* Cursor-style flyout to the right of the row. The tooltip wraps
            the row's inner content (a host <div>), NOT the menu item:
            DropdownMenuItem is a plain function component (no forwardRef),
            so TooltipTrigger's `asChild` ref can't attach to it under
            React 18 — the flyout wouldn't open and it logs ref warnings.
            Wrapping the <div> keeps refs working and the item a direct
            roving-focus child of DropdownMenuContent. No-ops when the
            agent has no description. */}
        <AgentRowTooltip agent={agent}>
          <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
            <span className="truncate">{agent.display_name}</span>
            {blurb && (
              <span className="truncate text-[11px] text-muted-foreground/70">{blurb}</span>
            )}
          </div>
        </AgentRowTooltip>
        {/* Compact right-aligned readiness pill; the full
            remediation text lives in the composer warning. */}
        {harnessUnconfiguredOnHost(agent.harness, harnessWarningHost) && (
          <Badge
            variant="outline"
            className="ml-auto self-center border-amber-300 bg-amber-50 text-[11px] text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
            data-testid={`new-chat-landing-agent-warning-${agent.id}`}
          >
            needs setup
          </Badge>
        )}
      </DropdownMenuItem>
    );
  };

  function selectHost(hostId: string) {
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
    if (sandboxSelected) return;
    // Mirror selectHost: a managed session's host and workspace are both
    // server-chosen, so clear any prior host pick and its workspace.
    setSandboxSelected(true);
    setSelectedHostId(null);
    setWorkspace("");
    seededHostRef.current = null;
  }

  async function handleCreate() {
    // Mirror the Send button's disabled condition (canSubmit) so the Enter-key
    // and form-submit paths that call this directly can't create a session with
    // a blank message, host, agent, or workspace.
    if (!canSubmit) return;
    setCreating(true);
    setCreateError(null);
    try {
      const trimmedBranch = branchName.trim();
      const agent = agentList.find((a) => a.id === effectiveAgentId);
      const nativeLabels = nativeWrapperLabelsForAgent(agent);
      const agentSupportsPermissionMode = nativeAgentHasCapability(agent, "permissionMode");
      const agentSupportsApprovalMode = nativeAgentHasCapability(agent, "approvalMode");
      const res = await authenticatedFetch("/v1/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: effectiveAgentId,
          // Managed (cloud sandbox) creates let the server provision the
          // host: the schema rejects host_id and path workspaces (and git
          // needs a host_id). The optional repository inputs compose into
          // the URL-form workspace the server clones; undefined (no repo)
          // is dropped by JSON.stringify.
          ...(sandboxSelected
            ? {
                host_type: "managed",
                workspace: composeSandboxWorkspace(sandboxRepoUrl, sandboxRepoBranch),
              }
            : {
                host_id: selectedHostId,
                workspace: workspaceTrimmed,
                git: trimmedBranch
                  ? { branch_name: trimmedBranch, base_branch: baseBranch.trim() || undefined }
                  : undefined,
              }),
          // Native terminal agents open terminal-first: `omnigent.ui:
          // terminal` tells the UI to render the terminal wrapper, and
          // `omnigent.wrapper` selects which CLI bridge the runner launches.
          // The values are the registered wrapper ids the runner keys off —
          // they must match the wrapper registry, not the agent display name.
          labels: nativeLabels,
          // Permission / approval mode → CLI flag pair, persisted as
          // terminal_launch_args. Omitted for the default and non-native agents.
          terminal_launch_args:
            agentSupportsPermissionMode && permissionMode !== CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
              ? ["--permission-mode", permissionMode]
              : agentSupportsApprovalMode && approvalMode !== CODEX_NATIVE_DEFAULT_APPROVAL_MODE
                ? (CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.args ?? [])
                : undefined,
          // Cost-control switch from the "Cost Optimized" pill; polly-only
          // (cost control is a polly feature) and omitted when unset so the
          // session defers to the spec default.
          cost_control_mode_override:
            agent?.name === "polly" ? (costControlMode ?? undefined) : undefined,
          // Brain-harness pick from the agent flyout. Omitted when the user
          // kept the spec default (pickedHarness is null) so the session
          // tracks the agent's declared harness.
          harness_override: pickedHarness ?? undefined,
        }),
      });
      if (!res.ok) {
        setCreateError(await describeCreateError(res));
        return;
      }
      const data = (await res.json()) as { id: string };
      // Sandbox creates have no user-picked workspace to remember.
      if (!sandboxSelected) addRecent(workspaceTrimmed);
      // Fire-and-forget: don't block navigation on the sidebar list refresh.
      // The background refetch (or the WS session_added push) backfills the
      // new session's row within ~1s of landing in the chat; the chat itself
      // loads from the session id and never reads the sidebar cache.
      void queryClient.refetchQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["directory-sessions"] });
      const initialPrompt = sanitizeInitialPrompt(message);
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
      navigate(`/c/${data.id}`);
    } catch {
      setCreateError("Couldn't reach the server. Check your connection and try again.");
    } finally {
      setCreating(false);
    }
  }

  // The working-directory chip — a single Popover trigger button that opens
  // the file browser. The directory-conflict warning lives inside the browser
  // (a banner on the occupied folder), not on the chip.
  const workspaceChip = (
    <button
      type="button"
      className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
      data-testid="new-chat-landing-workspace-chip"
    >
      <FolderIcon className="size-4 shrink-0" />
      <span className={`max-w-40 truncate ${workspaceTrimmed !== "" ? "text-foreground" : ""}`}>
        {workspaceLabel}
      </span>
      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
    </button>
  );

  return (
    // pb-12 lifts the content slightly above the geometric center, where
    // the hero reads better optically.
    <div className="flex flex-1 items-center justify-center" data-testid="new-chat-landing">
      {/* Padding lives inside the 840px cap, so the composer renders at
          840 − 80 = 760px max. */}
      <div className="flex w-full max-w-[840px] flex-col items-center gap-8 px-10 pt-8 pb-16">
        <div className="flex flex-col items-center gap-3.5 sm:flex-row">
          <OttoEyes className="h-18 w-auto shrink-0" />
          <h1 className="text-center text-3xl font-medium tracking-[-0.03em] text-foreground sm:text-left">
            What should we do?
          </h1>
        </div>
        <div className="relative flex w-full flex-col gap-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleCreate();
            }}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            // Two visual states only (no hover): resting --border, and
            // --foreground while the textarea itself has focus (has-[]
            // scopes it so focusing footer buttons doesn't trigger it).
            // dark:bg-card-solid: the footer tray below tucks its top
            // edge behind this card (-mt-9), and the dark glass --card
            // is 60% alpha — the tucked strip ghosts through a
            // translucent card. Mirrors the chat composer card. Drag-over
            // lifts an inset ring (overlay below).
            className={cn(
              "relative z-10 flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid shadow-[0_12px_20px_-20px_rgba(0,0,0,0.14),0_20px_28px_-28px_rgba(0,0,0,0.1)] transition-[border-color,box-shadow] duration-150 has-[textarea:focus]:border-foreground",
              isDragActive && "ring-2 ring-ring ring-inset",
            )}
            data-testid="new-chat-landing-composer"
          >
            {isDragActive && (
              <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
                <span className="text-sm font-medium text-ring">Drop files here</span>
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
            <textarea
              ref={textareaRef}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
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
              placeholder={pillSkills.length > 0 ? "" : "Describe a task to start a new session…"}
              aria-label="Describe a task to start a new session"
              rows={1}
              autoFocus
              data-testid="new-chat-landing-input"
              // Compose-pill text spec: SF Pro Text system stack at
              // 14px/20px. (Note: sub-16px inputs make mobile Safari
              // auto-zoom on focus — accepted tradeoff per the design.)
              // Heights are border-box (16px top + 4px bottom padding lives
              // inside them): min 60px = one 20px line + a spare line of
              // breathing room; max 200px = the spec's 180px of content.
              // useAutoGrowTextarea drives the height between the two.
              className="max-h-[200px] min-h-[60px] w-full resize-none overflow-y-auto bg-transparent px-4 pt-4 pb-1 font-['SF_Pro_Text',-apple-system,BlinkMacSystemFont,system-ui,sans-serif] text-sm leading-5 text-foreground outline-none placeholder:text-muted-foreground"
            />
            {/* Gated on an empty draft so it reads as the placeholder.
                pointer-events-none lets clicks fall through to focus the
                textarea; the pills themselves opt back in. */}
            {pillSkills.length > 0 && message.length === 0 && (
              <div className="pointer-events-none absolute inset-x-4 top-4 flex flex-wrap items-center gap-2">
                <span className="font-['SF_Pro_Text',-apple-system,BlinkMacSystemFont,system-ui,sans-serif] text-sm leading-5 text-muted-foreground">
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
            {/* File chips — shown below the textarea when files are attached. */}
            {files.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-4 pb-2">
                {files.map((file, i) => (
                  <span
                    key={i}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
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
            <div className="flex items-center justify-between pt-1 pr-4 pb-3 pl-2">
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
                  <PaperclipIcon className="size-4" />
                  <span className="sr-only">Attach files</span>
                </Button>
                <ComposerMicButton
                  disabled={creating}
                  onTranscript={(text) => setMessage((prev) => (prev ? `${prev} ${text}` : text))}
                />
              </div>
              <div className="flex items-center gap-0.5">
                {/* Polly-only surface — cost control is a polly feature, so
                    the toggle is hidden unless the selected agent is polly. */}
                {/* Temporarily hidden (#3021): re-enable by removing the false gate. */}
                {false && selectedAgent?.name === "polly" && (
                  // Mode-only variant: no verdict can exist before the session does.
                  <IntelligentModelControl value={costControlMode} onChange={setCostControlMode} />
                )}
                {agentList.length > 0 ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        data-testid="new-chat-landing-agent-select"
                        className="h-8 gap-1.5 px-2.5 text-muted-foreground hover:text-foreground"
                      >
                        <span className="max-w-20 truncate text-sm tabular-nums md:max-w-[18rem]">
                          {agentLabel}
                        </span>
                        <ChevronDownIcon className="size-3.5 opacity-60" />
                      </Button>
                    </DropdownMenuTrigger>
                    {/* side=bottom documents the intent: the menu is a short
                        agent list (harness / permission settings live in the
                        footer tray's Advanced menu), so it should always drop
                        downward like the other composer menus. */}
                    <DropdownMenuContent
                      align="end"
                      side="bottom"
                      className="max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-64 max-w-[calc(100vw-2rem)] overflow-y-auto p-1"
                    >
                      {/* Built-in agents first, then a divider, then any
                          custom (user-registered) agents. renderAgentRow is
                          defined once and reused for both groups. The divider
                          only renders when BOTH groups are non-empty, so a
                          deployment with only custom agents (or only built-ins)
                          never shows a leading/dangling separator. */}
                      {builtinAgents.map((agent) => renderAgentRow(agent))}
                      {builtinAgents.length > 0 && customAgents.length > 0 && (
                        <DropdownMenuSeparator />
                      )}
                      {customAgents.map((agent) => renderAgentRow(agent))}
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : (
                  <span className="text-xs text-muted-foreground">No agents</span>
                )}
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="inline-flex">
                        <Button
                          type="submit"
                          size="icon"
                          disabled={!canSubmit}
                          aria-label="Start session"
                          data-testid="new-chat-landing-submit"
                          className="size-8 rounded-full bg-foreground text-card transition-opacity hover:opacity-80 disabled:opacity-50"
                        >
                          <ArrowUpIcon className="size-4" />
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
          {/* Composer footer tray — host / working directory / worktree
              selectors. Renders below the pill at z-0 while the pill sits
              at z-10: -mt-9 cancels the wrapper's gap-3 (12px) and tucks
              the tray's top 24px underneath the pill's rounded bottom
              edge. Height is padding-driven (pt-8 + h-6 chips + pb-2 =
              the same 64px as before when the chips fit one row) so the
              chip row can wrap on narrow screens — with a fixed h-16 the
              chips overflowed the viewport on phones, widening the whole
              page (#sidebar-wider-than-screen on the landing page). */}
          <div className="relative z-0 -mt-9 flex w-full items-center rounded-b-2xl bg-tray/40 pt-8 pr-3 pb-2 pl-2">
            <div className="flex flex-wrap items-center gap-1.5">
              {/* Host chip */}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
                    data-testid="new-chat-landing-host-chip"
                  >
                    {isCloudHost ? (
                      <MonitorCloudIcon className="size-4 shrink-0" />
                    ) : (
                      <MonitorIcon className="size-4 shrink-0" />
                    )}
                    <span
                      className={`max-w-32 truncate ${sandboxSelected || selectedHost != null ? "text-foreground" : ""}`}
                    >
                      {hostLabel}
                    </span>
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
                          className="text-xs data-[active=true]:bg-accent/60"
                        >
                          <span className="flex items-center gap-2">
                            <MonitorCloudIcon className="size-4 text-muted-foreground" />
                            <span className="text-xs">{sandboxLabel}</span>
                          </span>
                        </DropdownMenuItem>
                      ) : (
                        <DropdownMenuItem
                          aria-disabled="true"
                          onSelect={(e) => e.preventDefault()}
                          className="flex items-center justify-between px-2 py-1.5 text-xs text-muted-foreground opacity-60"
                          data-testid="new-chat-landing-sandbox-option-disabled"
                        >
                          <span className="flex items-center gap-2">
                            <MonitorCloudIcon className="size-4 text-muted-foreground" />
                            <span className="text-xs">New Sandbox</span>
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
                  {allHosts.length === 0 && (
                    <div className="px-2 py-1.5 text-xs text-muted-foreground">
                      No hosts connected yet.
                    </div>
                  )}
                  {onlineHosts.map((host) => (
                    <DropdownMenuItem
                      key={host.host_id}
                      onSelect={() => selectHost(host.host_id)}
                      data-active={host.host_id === selectedHostId ? "true" : undefined}
                      className="text-xs data-[active=true]:bg-accent/60"
                    >
                      <HostOption host={host} />
                    </DropdownMenuItem>
                  ))}
                  {offlineHosts.map((host) => (
                    <DropdownMenuItem key={host.host_id} disabled className="text-xs">
                      <HostOption host={host} />
                    </DropdownMenuItem>
                  ))}
                  {allHosts.length > 0 && <DropdownMenuSeparator />}
                  {/* Persistent escape hatch: open the connect-a-host
                    instructions. Present even with zero hosts so a fresh user
                    is never stuck. */}
                  <DropdownMenuItem
                    onSelect={() => setConnectOpen(true)}
                    data-testid="new-chat-landing-connect-host"
                    className="gap-2 text-xs text-muted-foreground"
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
                      className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
                      data-testid="new-chat-landing-repo-chip"
                    >
                      <GitBranchIcon className="size-4 shrink-0" />
                      <span
                        className={`max-w-40 truncate ${sandboxRepoName ? "text-foreground" : "text-muted-foreground"}`}
                      >
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
                          className="text-xs font-medium text-foreground"
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
                        className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-repo-input"
                      />
                      <input
                        type="text"
                        value={sandboxRepoBranch}
                        onChange={(e) => setSandboxRepoBranch(e.target.value)}
                        placeholder="Branch (defaults to the repo's default)"
                        aria-label="Repository branch"
                        className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-repo-branch-input"
                      />
                      <p className="text-xs text-muted-foreground">
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
                        // occupy. Suppressed once a git branch is named — that
                        // starts an isolated worktree, so there's no shared-dir
                        // conflict regardless of the picked directory.
                        occupancyForPath={
                          branchName.trim() === ""
                            ? (abs) => occupancyByDir.get(normalizeWorkspacePath(abs) ?? "") ?? 0
                            : undefined
                        }
                      />
                    ) : (
                      <p className="p-3 text-xs text-muted-foreground">Select a host first.</p>
                    )}
                  </PopoverContent>
                </Popover>
              )}

              {/* Git worktree chip — hidden for sandbox sessions (worktree
                creation requires a caller-supplied host_id). */}
              {!sandboxSelected && (
                <Popover>
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
                      data-testid="new-chat-landing-branch-chip"
                    >
                      <GitBranchIcon className="size-4 shrink-0" />
                      <span
                        className={`max-w-32 truncate ${branchName.trim() ? "text-foreground" : ""}`}
                      >
                        {worktreeLabel}
                      </span>
                      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
                    </button>
                  </PopoverTrigger>
                  <PopoverContent align="start" className="w-[min(20rem,calc(100vw-2rem))] p-3">
                    <div className="flex flex-col gap-2">
                      <label
                        htmlFor="landing-branch-name"
                        className="text-xs font-medium text-foreground"
                      >
                        Git worktree branch (optional)
                      </label>
                      <input
                        id="landing-branch-name"
                        type="text"
                        value={branchName}
                        onChange={(e) => setBranchName(e.target.value)}
                        placeholder="feature/my-branch"
                        className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-branch-input"
                      />
                      {branchName.trim() !== "" && (
                        <input
                          type="text"
                          value={baseBranch}
                          onChange={(e) => setBaseBranch(e.target.value)}
                          placeholder="Base branch (defaults to current branch)"
                          aria-label="Base branch"
                          className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                          data-testid="new-chat-landing-base-branch-input"
                        />
                      )}
                      <p className="text-xs text-muted-foreground">
                        Creates an isolated git worktree for a new branch. Leave blank to start
                        directly in the working directory.
                      </p>
                    </div>
                  </PopoverContent>
                </Popover>
              )}

              {/* Advanced settings chip — per-agent knobs that don't warrant
                their own chip: the brain-harness override (bundle agents),
                Claude Code's permission mode, and Codex's approval mode.
                Hidden when the selected agent has none. */}
              {(selectedAgentDefaultHarness != null ||
                supportsPermissionMode ||
                supportsApprovalMode) && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      type="button"
                      className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
                      data-testid="new-chat-landing-advanced-chip"
                    >
                      <SettingsIcon className="size-4 shrink-0" />
                      <span className="truncate">Advanced</span>
                      <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="start"
                    className="max-h-[var(--radix-dropdown-menu-content-available-height)] w-64 max-w-[calc(100vw-2rem)] overflow-y-auto p-1"
                  >
                    {selectedAgentDefaultHarness != null && (
                      <BrainHarnessOptions
                        value={pickedHarness ?? selectedAgentDefaultHarness}
                        onValueChange={(h) =>
                          // Picking the spec default clears the override so the
                          // session tracks the spec.
                          setPickedHarness(h === selectedAgentDefaultHarness ? null : h)
                        }
                        host={harnessWarningHost}
                      />
                    )}
                    {/* Permission mode (Claude Code only) — claude-native has no
                      overridable harness, so the two sections never co-render
                      today; the separator covers a future agent with both. */}
                    {supportsPermissionMode && (
                      <>
                        {selectedAgentDefaultHarness != null && <DropdownMenuSeparator />}
                        <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
                          Permission mode
                        </div>
                        <PermissionModeOptions
                          value={permissionMode}
                          onValueChange={setPermissionMode}
                        />
                      </>
                    )}
                    {/* Approval mode (Codex only) — codex-native has no
                      overridable harness, so the two sections never co-render
                      today; the separator covers a future agent with both. */}
                    {supportsApprovalMode && (
                      <>
                        {(selectedAgentDefaultHarness != null || supportsPermissionMode) && (
                          <DropdownMenuSeparator />
                        )}
                        <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
                          Approval mode
                        </div>
                        <ApprovalModeOptions value={approvalMode} onValueChange={setApprovalMode} />
                      </>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          </div>

          {/* Warn (don't block) when the selected agent's harness isn't
              configured on the selected host — the host re-checks at
              launch, so submitting surfaces a specific error if it
              really can't run. Normal-flow directly under the composer
              (like the createError line below) so it reads as part of it. */}
          {selectedAgentUnconfigured && (
            <p
              className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-500"
              data-testid="new-chat-landing-harness-warning"
            >
              <TriangleAlertIcon className="size-3.5 shrink-0" />
              <span>
                {selectedAgent?.display_name} isn&apos;t configured on {harnessWarningHost?.name} —
                run <code>omnigent setup</code> on that machine.
              </span>
            </p>
          )}

          {createError && (
            <p className="text-xs text-destructive" data-testid="new-chat-landing-error">
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
    </div>
  );
}
