// Subagents tab content for the right-side rail. Renders the session
// tree under the root conversation — a "main" link back to the root,
// then its sub-agent sessions recursively (children, grandchildren,
// …) down to ``MAX_TREE_DEPTH`` levels, each level indented one step
// further. The user can move between any agents in the tree without
// leaving the rail.
//
// The active session may itself be a descendant (the user clicked
// into a sub-agent). The rail still renders the tree from the
// top-level root, with the active row highlighted. AppShell resolves
// the root id (walking the parent chain) and passes it as
// ``rootSessionId``.
//
// Each row is a Link to the target conversation page so cmd/middle-
// click opens it in a new tab, matching the sidebar's behavior.

import { useState } from "react";
import type { ComponentType, SVGProps } from "react";
import {
  BookOpenIcon,
  BotIcon,
  Code2Icon,
  CompassIcon,
  CornerDownRightIcon,
  FileTextIcon,
  FlaskConicalIcon,
  PlusIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Badge } from "@/components/ui/badge";
import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { GooseIcon } from "@/components/icons/GooseIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { RunningDot } from "@/components/RunningDot";
import { MAX_TREE_DEPTH, useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import type { SessionItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { AddAgentDialog } from "./AddAgentDialog";

// Session-scoped URL params that the file viewer / Files panel write
// for one session and AppShell's restore effect re-reads on the next.
// Stripping these on rail navigation prevents a sticky ``?file=`` from
// the previous session yanking the user into the file viewer of the
// next one. Other params (e.g. ``?debug=1`` for ``useDebugMode``) are
// global and must be preserved across navigation.
const SESSION_SCOPED_PARAMS = ["file", "diff", "comment", "view"] as const;
const CODEX_NATIVE_SUBAGENT_WRAPPER = "codex-native-ui-subagent";
const OPENCODE_NATIVE_SUBAGENT_WRAPPER = "opencode-native-ui-subagent";
// Pi children are scaffold (no wrapper label); the spawn title's agent-type head (``tool``) is the signal.
const PI_AGENT_NAME = "pi";
type AgentRowIcon = ComponentType<SVGProps<SVGSVGElement>>;

/**
 * Build a rail-link search string from the current URL, dropping the
 * session-scoped params and keeping anything else.
 *
 * @param search - The current ``location.search`` string,
 *   e.g. ``"?file=foo.txt&debug=1"``.
 * @returns A search string suitable for a ``<Link to={{ search }}>``,
 *   e.g. ``"?debug=1"`` or ``""`` when nothing remains.
 */
function railLinkSearch(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of SESSION_SCOPED_PARAMS) params.delete(key);
  const next = params.toString();
  return next ? `?${next}` : "";
}

interface SubagentsPanelProps {
  /** The conversation currently rendered in main. Used only to
   *  highlight the active row. */
  conversationId: string;
  /** Root (parent) session whose children populate the list. When the
   *  user is on a top-level session this is the active id; when on a
   *  child it is the child's parent id. AppShell resolves this from
   *  ``activeSession.parentSessionId``. */
  rootSessionId: string;
}

export function SubagentsPanel({ conversationId, rootSessionId }: SubagentsPanelProps) {
  // Every list in the tree polls at TREE_POLL_MS as a staleness floor;
  // stream pushes remain the fast path. The stream only carries
  // ``session.child_session.updated`` for the *streamed* (active)
  // session's direct children — deeper levels, and the whole tree when
  // the user is viewing a descendant, have no live channel, so without
  // the poll their status would freeze at the snapshot. A child can be
  // busy even when its parent is "idle" (parent parked awaiting the
  // child); the poll + stream together surface that.
  const { children, isLoading, error } = useChildSessions(rootSessionId, TREE_POLL_MS);
  const [addOpen, setAddOpen] = useState(false);

  // Loading/error states only surface when there's no cached data to
  // show alongside the "main" row. Once any data is available we
  // render the list and let polling refresh it transparently.
  if (isLoading && children.length === 0) {
    return (
      <div className="flex h-full flex-1 items-center justify-center px-4 py-8 text-center text-xs text-muted-foreground bg-card">
        Loading…
      </div>
    );
  }
  if (error && children.length === 0) {
    return (
      <div className="flex h-full flex-1 items-center justify-center px-4 py-8 text-center text-xs text-muted-foreground bg-card">
        Failed to load agents.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <button
        type="button"
        data-testid="add-agent-button"
        onClick={() => setAddOpen(true)}
        className="hidden"
      >
        <PlusIcon className="size-3.5 shrink-0" />
        Add agent
      </button>
      <ul className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1">
        <MainRow rootSessionId={rootSessionId} isActive={conversationId === rootSessionId} />
        {children.map((child) => (
          <SubagentRow key={child.id} child={child} depth={1} conversationId={conversationId} />
        ))}
      </ul>
      {/* Mounted only while open so a closed rail issues no /v1/agents
          fetch and carries none of the dialog's query dependencies. */}
      {addOpen && (
        <AddAgentDialog parentSessionId={rootSessionId} open={addOpen} onOpenChange={setAddOpen} />
      )}
    </div>
  );
}

// Collapsed activity of an agent, shared by the main row (derived from the
// parent session's snapshot status) and the child rows (derived from
// ``busy`` + ``current_task_status``). Drives the dot tone, whether the
// label word shows, and whether the row is de-emphasized. ``awaiting`` =
// parked on an approval / input prompt and needs the user's attention.
type AgentActivity = "launching" | "working" | "awaiting" | "done" | "failed" | "idle" | "other";

interface AgentStatus {
  activity: AgentActivity;
  /** Human label, shown inline for notable states and always in the tooltip. */
  label: string;
  /** Optional detail for the tooltip / accessible label. */
  details?: string;
}

function firstErrorLine(message: string): string {
  const first = message
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
  return first ?? message;
}

/**
 * Resolve a child session's display status.
 *
 * @param child - One child-session summary from the poll.
 * @returns The collapsed activity + its label, e.g.
 *   ``{ activity: "working", label: "Working" }``.
 */
function childStatus(child: ChildSessionInfo): AgentStatus {
  // Awaiting input outranks ``busy``: a sub-agent parked on an
  // elicitation is still "running" its turn (the future is pending),
  // so checking ``busy`` first would hide the prompt behind a generic
  // "Working" pill — exactly the signal the user needs to act on.
  if (child.pending_elicitations_count > 0) {
    return { activity: "awaiting", label: "Needs response" };
  }
  // ``busy`` is the authoritative live flag (queued or in_progress);
  // ``current_task_status`` may be "launching", "completed", "failed",
  // "cancelled", or null when no task has run yet.
  if (child.current_task_status === "launching") {
    return { activity: "launching", label: "Launching" };
  }
  if (child.busy) return { activity: "working", label: "Working" };
  if (child.last_task_error) {
    return {
      activity: "failed",
      label: "Failed",
      details: firstErrorLine(child.last_task_error.message),
    };
  }
  if (child.current_task_status === "failed") return { activity: "failed", label: "Failed" };
  if (child.current_task_status === "completed") return { activity: "done", label: "Done" };
  if (child.current_task_status) {
    return { activity: "other", label: child.current_task_status };
  }
  return { activity: "idle", label: "Idle" };
}

/**
 * Resolve the parent ("main") session's display status from its snapshot.
 *
 * @param status - ``session.status`` from the snapshot, e.g. ``"running"``,
 *   or ``undefined`` while the snapshot is still loading.
 * @returns The collapsed activity + its label.
 */
function sessionStatus(status: string | undefined): AgentStatus {
  if (status === "launching") return { activity: "launching", label: "Launching" };
  if (status === "running") return { activity: "working", label: "Working" };
  if (status === "failed") return { activity: "failed", label: "Failed" };
  return { activity: "idle", label: "Idle" };
}

// Dot color per dot-rendered state. Working uses the animated RunningDot
// and awaiting uses the "Needs response" tag, so both are excluded here.
// "done" is a quiet, expected outcome, so it reads as a muted dot rather
// than a loud green one.
const DOT_TONE: Record<Exclude<AgentActivity, "working" | "awaiting">, string> = {
  done: "bg-muted-foreground/55",
  failed: "bg-destructive",
  idle: "bg-muted-foreground/55",
  launching: "bg-muted-foreground/70",
  other: "bg-muted-foreground/55",
};

// Quiet states show only an indicator — the word lives in the tooltip — so the
// row stays clean. Working is quiet too: the pulsing pink dot already reads as
// "active", so the redundant "Working" label is dropped. The eye still lands on
// agents that need input or are in trouble, which keep their word.
const QUIET_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: true,
  awaiting: false,
  failed: false,
  other: false,
  done: true,
  idle: true,
};

// Settled states are de-emphasized (dimmed) so live agents dominate the list.
// Kept separate from QUIET_STATE: ``working`` is quiet (no label word) but must
// NOT be dimmed — an actively-working agent should stay full-strength.
const SETTLED_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: false,
  awaiting: false,
  failed: false,
  other: false,
  done: true,
  idle: true,
};

/**
 * Map a sub-agent type label to a category icon so a mix of agents reads by
 * role at a glance (Claude Code spawns many same-type "Explore" agents — the
 * icon distinguishes roles; the preview line below distinguishes instances).
 * Category icons are monochrome — the row applies the muted color; the
 * fallback is the full-color Otto (starfish) mascot.
 *
 * @param tool - The agent type, e.g. ``"Explore"`` or ``"researcher"``;
 *   ``null`` when the child carries no type.
 * @returns An SVG icon component.
 */
export function iconForAgentType(tool: string | null): AgentRowIcon {
  const t = (tool ?? "").toLowerCase();
  if (t.includes("explore")) return SearchIcon;
  if (t.includes("research")) return BookOpenIcon;
  if (t.includes("plan") || t.includes("architect")) return CompassIcon;
  if (t.includes("review")) return ScanSearchIcon;
  if (t.includes("test")) return FlaskConicalIcon;
  if (t.includes("doc") || t.includes("writ")) return FileTextIcon;
  if (
    t.includes("code") ||
    t.includes("eng") ||
    t.includes("dev") ||
    t.includes("front") ||
    t.includes("back")
  ) {
    return Code2Icon;
  }
  return OttoIcon;
}

/**
 * Pick a brand glyph for coding child sessions when the summary carries
 * enough identity metadata. Native children identify via their wrapper
 * label (authoritative — a custom scaffold agent merely *named* "codex"
 * must not get the Codex logo). Pi children are scaffold sessions with
 * no wrapper label, so the exact agent name ``"pi"`` is the signal.
 *
 * Only full native sessions get the brand glyph. *Sub-agent* wrapper
 * children (``…-subagent``) deliberately fall through to the role icons
 * (and the Otto fallback) — a native session's sub-agents are all the
 * same brand, so repeating the logo down the tree says nothing, while
 * role icons distinguish what each one is doing.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The Claude/Codex/pi glyph component, or ``null`` for generic agents.
 */
function brandChildIcon(child: ChildSessionInfo): AgentRowIcon | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  if (nativeAgent?.iconKind === "claude") return ClaudeIcon;
  if (nativeAgent?.iconKind === "codex") return CodexIcon;
  if (nativeAgent?.iconKind === "opencode") return OpenCodeIcon;
  if (nativeAgent?.iconKind === "pi") return PiIcon;
  if (nativeAgent?.iconKind === "cursor") return CursorIcon;
  if (nativeAgent?.iconKind === "kiro") return CursorIcon;
  if (nativeAgent?.iconKind === "antigravity") return AntigravityIcon;
  if (nativeAgent?.iconKind === "goose") return GooseIcon;
  if (nativeAgent?.iconKind === "kimi") return KimiIcon;
  // Exact match — substring checks would false-match names like "pipeline".
  if (child.tool === PI_AGENT_NAME) return PiIcon;
  return null;
}

/**
 * Indicator + optional label shared by the main and child rows. The working
 * state reuses the sidebar's RunningDot in the same brand-pink tone, so
 * "active" reads identically across the app; other states are a single
 * tokenized dot.
 *
 * The indicator is rendered last (label first) so that, with the indicator
 * right-aligned in the row, every row's dot lands in the same column
 * regardless of label width or whether the label is shown — otherwise a
 * wide label like "Failed" pushes its dot left of a bare "Idle" dot.
 *
 * @param status - The resolved activity + label to render.
 */
function StatusIndicator({ activity, label, details }: AgentStatus) {
  const title = details ? `${label}: ${details}` : label;
  // Awaiting renders the exact same "Needs response" tag as the sidebar
  // (SessionStateBadge) so the approval affordance reads identically across
  // the app. The tag carries its own copy, so the row's separate label word
  // is omitted to avoid duplicating the text.
  if (activity === "awaiting") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center text-xs"
      >
        <Badge className="border-transparent bg-warning/15 text-warning">Needs response</Badge>
      </span>
    );
  }
  if (activity === "failed") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center gap-1 text-destructive text-xs"
      >
        <span>{label}</span>
        <span className={cn("inline-block size-2 shrink-0 rounded-full", DOT_TONE.failed)} />
      </span>
    );
  }
  return (
    <span
      aria-label={title}
      title={title}
      data-testid="subagent-status-dot"
      className="inline-flex shrink-0 items-center gap-1 text-muted-foreground text-xs"
    >
      {!QUIET_STATE[activity] && <span>{label}</span>}
      {activity === "working" ? (
        <RunningDot />
      ) : (
        <span className={cn("inline-block size-2 shrink-0 rounded-full", DOT_TONE[activity])} />
      )}
    </span>
  );
}

/**
 * Pick the primary label for a child-session row.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The label shown beside the child icon.
 */
function childPrimaryLabel(child: ChildSessionInfo): string {
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const childWrapper = child.labels?.[WRAPPER_LABEL_KEY];
  const isNativeSubagent =
    childWrapper === CODEX_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === OPENCODE_NATIVE_SUBAGENT_WRAPPER;
  if (isNativeSubagent && !isUserAdded) {
    return child.tool ?? child.title ?? child.id;
  }
  let titleTask: string | null = null;
  if (child.title?.includes(":")) {
    const titleSuffix = child.title.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  return child.session_name ?? titleTask ?? child.title ?? child.tool ?? child.id;
}

/**
 * First row of the Subagents list — a navigation link back to the
 * parent (root) session. Always present, even when the parent has
 * no children, so the rail is a complete navigation surface for the
 * parent-children tree.
 *
 * The leading icon doubles as the agent-kind indicator: a Claude or
 * Codex glyph for the native wrappers, and a generic bot icon for
 * everything else. Sub-agent rows nest below
 * with their own role icons, so the "main vs sub-agent" distinction
 * is carried by position + nesting connector rather than a pill.
 */
// Cap matches the server's child-session preview so the main row reads
// consistently with the child rows (CSS truncates to one line regardless;
// this just keeps the DOM string bounded).
const MAIN_PREVIEW_MAX_CHARS = 150;

/**
 * Derive a one-line preview of the root session's most recent message from
 * its snapshot items, mirroring the server's child-session preview so the
 * "main" row reads like the child rows below it.
 *
 * Scans newest-first for the last ``message`` item and joins its text
 * content blocks (assistant ``output_text`` / user ``input_text``).
 *
 * @param items - The root session's snapshot items (oldest-first), or
 *   ``undefined`` while the snapshot is still loading.
 * @returns The latest message text, trimmed and length-capped, or ``null``
 *   when the session has no message item yet.
 */
function mainMessagePreview(items: SessionItem[] | undefined): string | null {
  if (!items) return null;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type !== "message") continue;
    const content = (item as { data?: { content?: unknown } }).data?.content;
    if (!Array.isArray(content)) continue;
    const text = content
      .map((block) =>
        block && typeof block === "object" && "text" in block
          ? String((block as { text: unknown }).text)
          : "",
      )
      .join("")
      .trim();
    if (text) {
      return text.length > MAIN_PREVIEW_MAX_CHARS
        ? `${text.slice(0, MAIN_PREVIEW_MAX_CHARS)}…`
        : text;
    }
  }
  return null;
}

/**
 * Resolve a session's brand icon from its native-wrapper ``iconKind``
 * (authoritative for native-terminal sessions) with a harness-substring
 * fallback for plain SDK sessions that carry no wrapper label — e.g.
 * ``omni --harness kimi``, whose ``harness: "kimi"`` would otherwise fall
 * through to the generic bot. Mirrors ``iconForAgent`` in ``AgentCard.tsx``.
 */
function iconForWrapperOrHarness(
  iconKind: string | undefined,
  harness: string | null | undefined,
  isNessie: boolean,
): AgentRowIcon {
  if (iconKind === "claude" || harness?.includes("claude")) return ClaudeIcon;
  if (iconKind === "codex" || harness?.includes("codex")) return CodexIcon;
  if (iconKind === "opencode" || harness?.includes("opencode")) return OpenCodeIcon;
  if (iconKind === "cursor" || iconKind === "kiro" || harness?.includes("cursor"))
    return CursorIcon;
  if (iconKind === "goose" || harness?.includes("goose")) return GooseIcon;
  if (iconKind === "kimi" || harness?.includes("kimi")) return KimiIcon;
  if (iconKind === "antigravity" || harness?.includes("antigravity")) return AntigravityIcon;
  // Exact match — a substring check would false-match e.g. "openapi".
  if (iconKind === "pi" || harness === "pi") return PiIcon;
  if (isNessie) return NessieIcon;
  return BotIcon;
}

function MainRow({ rootSessionId, isActive }: { rootSessionId: string; isActive: boolean }) {
  const { session } = useSession(rootSessionId);
  const search = railLinkSearch(useLocation().search);
  // Same wrapper-label probe used by the sidebar (Sidebar.tsx) and
  // TerminalFirstContext to decide a session is claude/codex-native.
  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  const isNessie = session?.agentName === "nessie";
  const Icon = iconForWrapperOrHarness(nativeAgent?.iconKind, session?.harness, isNessie);
  // Native wrappers show the product name (mirroring the sidebar) instead
  // of the spec's YAML name (e.g. "claude-native-ui"); other agents show
  // their agent name, with "main" only while the session loads or when it
  // carries no name.
  const label = nativeAgent?.displayName ?? session?.agentName ?? "main";
  const preview = mainMessagePreview(session?.items);
  return (
    <li>
      <Link
        // Drop session-scoped params (``file``, ``diff``, ``comment``,
        // ``view``) when navigating in the rail — those are tied to
        // one session's file-viewer state and must not bleed into the
        // next. Global params like ``?debug=1`` are preserved by
        // ``railLinkSearch`` so debug mode stays on across navigation.
        to={{ pathname: `/c/${rootSessionId}`, search }}
        data-testid="subagent-main-row"
        data-root-session-id={rootSessionId}
        data-agent-kind={
          nativeAgent != null ? `${nativeAgent.key}-native` : isNessie ? "nessie" : "agent"
        }
        className={cn(
          "flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60",
          isActive && "bg-accent",
        )}
      >
        <div className="flex w-full items-center gap-1">
          <Icon className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="shrink-0 truncate text-xs font-medium">{label}</span>
          <span className="flex-1" />
          <StatusIndicator {...sessionStatus(session?.status)} />
        </div>
        {preview && (
          // Indented to align with the title text above: 14px icon + 4px gap.
          <p
            data-testid="subagent-main-preview"
            className="truncate pl-[18px] text-[11px] text-muted-foreground"
          >
            {preview}
          </p>
        )}
      </Link>
    </li>
  );
}

// Staleness-floor poll interval for every child list in the tree. See
// the comment in SubagentsPanel — only the streamed session's direct
// children get live pushes, so the rest of the tree relies on this.
const TREE_POLL_MS = 15_000;

// Indentation: depth 1 keeps the original 24px gutter (pl-6); each
// further level steps in by another 14px so the connector glyphs read
// as a tree.
const ROW_BASE_PADDING_PX = 24;
const ROW_DEPTH_STEP_PX = 14;

function SubagentRow({
  child,
  depth,
  conversationId,
}: {
  child: ChildSessionInfo;
  /** Levels below the root, 1 = direct child of "main". */
  depth: number;
  /** The conversation currently rendered in main, for row highlighting. */
  conversationId: string;
}) {
  const status = childStatus(child);
  const search = railLinkSearch(useLocation().search);
  const Icon = brandChildIcon(child) ?? iconForAgentType(child.tool);
  const primary = childPrimaryLabel(child);
  const isActive = conversationId === child.id;
  // De-emphasize settled rows (done/idle) so working/failed agents dominate
  // — but never the row the user is currently viewing.
  const dim = !isActive && SETTLED_STATE[status.activity];
  // This child's own sub-agents, rendered as the next tree level.
  // Disabled (null id) at the depth cap so the fan-out of fetches is
  // bounded; ``useChildSessions`` skips the query entirely for null.
  const { children: grandchildren } = useChildSessions(
    depth < MAX_TREE_DEPTH ? child.id : null,
    TREE_POLL_MS,
  );
  return (
    <>
      <li>
        <Link
          // See MainRow: drop session-scoped params on rail navigation
          // (preserving global ones like ``?debug=1``) so a sticky
          // ``?file=`` from the previous session doesn't carry over.
          to={{ pathname: `/c/${child.id}`, search }}
          data-testid="subagent-row"
          data-child-session-id={child.id}
          data-depth={depth}
          // Left gutter (depth-stepped) + connector glyph nests this row
          // under its parent, signaling where it sits in the tree.
          style={{ paddingLeft: ROW_BASE_PADDING_PX + (depth - 1) * ROW_DEPTH_STEP_PX }}
          className={cn(
            "flex w-full flex-col gap-0.5 py-2 pr-2.5 text-left hover:bg-accent/60",
            isActive && "bg-accent",
            dim && "opacity-60 hover:opacity-100",
          )}
        >
          <div className="flex w-full items-center gap-1">
            <CornerDownRightIcon
              // Decorative nesting connector — the role icon beside it carries
              // the meaning, so hide this from the accessibility tree.
              aria-hidden="true"
              className="-ml-3 size-3 shrink-0 text-muted-foreground/60"
            />
            <Icon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="shrink-0 truncate text-xs font-medium">{primary}</span>
            <span className="flex-1" />
            <StatusIndicator {...status} />
          </div>
          {child.last_message_preview && (
            // Preview indented to align with the title text on the row
            // above: 12px connector - 12px (-ml-3) + 4px gap + 14px bot
            // icon + 4px gap = 22px. Relative to the row's own padding,
            // so it tracks the depth-stepped gutter automatically.
            <p className="truncate pl-[22px] text-[11px] text-muted-foreground">
              {child.last_message_preview}
            </p>
          )}
        </Link>
      </li>
      {grandchildren.map((grandchild) => (
        <SubagentRow
          key={grandchild.id}
          child={grandchild}
          depth={depth + 1}
          conversationId={conversationId}
        />
      ))}
    </>
  );
}
