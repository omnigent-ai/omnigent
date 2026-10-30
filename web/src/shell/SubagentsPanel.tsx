// Related sessions appear as peers, each with its own collapsible helper tree.
// Older servers keep the recursive subagent view. AppShell resolves the group root.

import { lazy, Suspense, useContext, useEffect, useState } from "react";
import type { ComponentType, SVGProps } from "react";
import {
  BookOpenIcon,
  BotIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleDotIcon,
  CircleHelpIcon,
  Code2Icon,
  CompassIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CornerDownRightIcon,
  EllipsisIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ListIcon,
  NetworkIcon,
  PauseIcon,
  PanelRightOpenIcon,
  ArrowUpIcon,
  ArrowDownIcon,
  PencilIcon,
  ArchiveIcon,
  Trash2Icon,
  PlusIcon,
  ScanSearchIcon,
  SearchIcon,
  UnplugIcon,
} from "lucide-react";
import { Link, useLocation, useNavigate } from "@/lib/routing";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { RunningDot } from "@/components/RunningDot";
import { shortModelName } from "@/components/CostRoutingControl";
import {
  MAX_TREE_DEPTH,
  useChildSessions,
  childSessionsQueryKey,
  cachedTreeContains,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import type { SessionItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { useQueryClient } from "@tanstack/react-query";
import { useArchiveRelatedSessions } from "@/hooks/useRelatedSessionLifecycle";
import { renameConversation, useStopAndDeleteConversation } from "@/hooks/useConversations";
import { SubagentPanelOpenerContext, useOpenSubagentInPanel } from "./subagentPanelOpener";

const SubagentsGraphView = lazy(() =>
  import("./SubagentsGraphView").then((m) => ({ default: m.SubagentsGraphView })),
);
import {
  ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER,
  CODEX_NATIVE_SUBAGENT_WRAPPER,
  nativeCodingAgentForWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { childPrimaryLabel } from "./subagentLabel";
import { childStatus, sessionStatus, type AgentActivity, type AgentStatus } from "./subagentStatus";
import { AddAgentDialog } from "./AddAgentDialog";

const CODEX_NATIVE_SUBAGENT_ROLE_LABEL = "omnigent.codex_native.agent_role";
const ANTIGRAVITY_NATIVE_SUBAGENT_ROLE_LABEL = "omnigent.antigravity_native.agent_role";
// Pi children are scaffold (no wrapper label); the spawn title's agent-type head (``tool``) is the signal.
const PI_AGENT_NAME = "pi";
type AgentRowIcon = ComponentType<SVGProps<SVGSVGElement>>;

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

type ViewMode = "list" | "graph";

export function SubagentsPanel({ conversationId, rootSessionId }: SubagentsPanelProps) {
  const { children, isLoading, error } = useChildSessions(rootSessionId);
  const relatedSessions = isFeatureEnabled(useServerInfo(), "related_sessions");
  const { session: sourceSession } = useSession(relatedSessions ? conversationId : null);
  const canCreate = relatedSessions && sourceSession?.permissionLevel === 4;
  const [addOpen, setAddOpen] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [collapsedRows, setCollapsedRows] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      (readSessionWorkspaceState(rootSessionId).collapsedSubagentIds ?? []).map((id) => [id, true]),
    ),
  );
  useEffect(() => {
    setCollapsedRows(
      Object.fromEntries(
        (readSessionWorkspaceState(rootSessionId).collapsedSubagentIds ?? []).map((id) => [
          id,
          true,
        ]),
      ),
    );
  }, [rootSessionId]);
  const toggleCollapsedRow = (id: string) => {
    setCollapsedRows((current) => {
      const next = { ...current, [id]: !current[id] };
      writeSessionWorkspaceState(rootSessionId, {
        collapsedSubagentIds: Object.keys(next).filter((key) => next[key]),
      });
      return next;
    });
  };

  // Loading/error states only surface when there's no cached data to
  // show alongside the "main" row.
  if (isLoading && children.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <ViewModeToggle viewMode={viewMode} onViewModeChange={setViewMode} />
        <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      </div>
    );
  }
  if (error && children.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <ViewModeToggle viewMode={viewMode} onViewModeChange={setViewMode} />
        <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
          Failed to load agents.
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <ViewModeToggle viewMode={viewMode} onViewModeChange={setViewMode} />
      <button
        type="button"
        data-testid="add-agent-button"
        onClick={() => setAddOpen(true)}
        className={
          canCreate
            ? "mx-2 mt-2 flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
            : "hidden"
        }
      >
        <PlusIcon className="size-3.5 shrink-0" />
        {relatedSessions ? "New session" : "Add agent"}
      </button>
      {viewMode === "graph" ? (
        <Suspense
          fallback={
            <div className="flex h-full flex-1 items-center justify-center text-sm text-muted-foreground">
              Loading graph…
            </div>
          }
        >
          <SubagentsGraphView conversationId={conversationId} rootSessionId={rootSessionId} />
        </Suspense>
      ) : (
        <ul className="relative flex min-h-0 flex-1 flex-col gap-px overflow-y-auto px-2 pb-2 pt-2">
          {relatedSessions ? (
            <RelatedSessionsList
              rootSessionId={rootSessionId}
              conversationId={conversationId}
              sessions={children}
              collapsedRows={collapsedRows}
              onToggleCollapsed={toggleCollapsedRow}
              canEdit={canCreate}
            />
          ) : (
            <>
              <MainRow rootSessionId={rootSessionId} isActive={conversationId === rootSessionId} />
              {children.map((child, index) => (
                <SubagentRow
                  key={child.id}
                  child={child}
                  depth={1}
                  rootGuideContinues={index < children.length - 1}
                  conversationId={conversationId}
                  collapsedRows={collapsedRows}
                  onToggleCollapsed={toggleCollapsedRow}
                />
              ))}
            </>
          )}
        </ul>
      )}
      {/* Mounted only while open so a closed rail issues no /v1/agents
          fetch and carries none of the dialog's query dependencies. */}
      {addOpen && (
        <AddAgentDialog
          parentSessionId={relatedSessions ? conversationId : rootSessionId}
          rootSessionId={rootSessionId}
          relatedSession={relatedSessions}
          open={addOpen}
          onOpenChange={setAddOpen}
        />
      )}
    </div>
  );
}

function RelatedSessionsList({
  rootSessionId,
  conversationId,
  sessions,
  collapsedRows,
  onToggleCollapsed,
  canEdit,
}: {
  rootSessionId: string;
  conversationId: string;
  sessions: ChildSessionInfo[];
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
  canEdit: boolean;
}) {
  const [order, setOrder] = useState(
    () => readSessionWorkspaceState(rootSessionId).relatedSessionOrder ?? [],
  );
  useEffect(
    () => setOrder(readSessionWorkspaceState(rootSessionId).relatedSessionOrder ?? []),
    [rootSessionId],
  );
  const peers = sessions.filter((child) => child.labels?.["omnigent.session_role"] === "peer");
  const rank = (id: string) => (order.includes(id) ? order.indexOf(id) : order.length);
  peers.sort((a, b) => rank(a.id) - rank(b.id));
  const active = peers.filter((child) => !child.archived);
  const archived = peers.filter((child) => child.archived);
  function move(id: string, direction: number) {
    const ids = active.map((child) => child.id);
    const index = ids.indexOf(id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setOrder(ids);
    writeSessionWorkspaceState(rootSessionId, { relatedSessionOrder: ids });
  }
  return (
    <>
      <MainRow
        rootSessionId={rootSessionId}
        isActive={conversationId === rootSessionId}
        fullSession
      />
      <HelperSection
        sessionId={rootSessionId}
        label="Main"
        sessions={sessions.filter(
          (child) => child.labels?.["omnigent.session_role"] !== "peer" && !child.archived,
        )}
        conversationId={conversationId}
        collapsedRows={collapsedRows}
        onToggleCollapsed={onToggleCollapsed}
      />
      {active.map((peer, index) => (
        <PeerSessionSection
          key={peer.id}
          peer={peer}
          rootSessionId={rootSessionId}
          conversationId={conversationId}
          collapsedRows={collapsedRows}
          onToggleCollapsed={onToggleCollapsed}
          canEdit={canEdit}
          onMoveUp={index > 0 ? () => move(peer.id, -1) : undefined}
          onMoveDown={index < active.length - 1 ? () => move(peer.id, 1) : undefined}
        />
      ))}
      {archived.length > 0 && (
        <li className="mt-3 px-2 text-xs font-medium text-muted-foreground">Archived sessions</li>
      )}
      {archived.map((peer) => (
        <PeerSessionSection
          key={peer.id}
          peer={peer}
          rootSessionId={rootSessionId}
          conversationId={conversationId}
          collapsedRows={collapsedRows}
          onToggleCollapsed={onToggleCollapsed}
          canEdit={canEdit}
        />
      ))}
    </>
  );
}

function PeerSessionSection({
  peer,
  rootSessionId,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
  canEdit,
  onMoveUp,
  onMoveDown,
}: {
  peer: ChildSessionInfo;
  rootSessionId: string;
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
  canEdit: boolean;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const { children } = useChildSessions(peer.archived ? null : peer.id);
  const archive = useArchiveRelatedSessions();
  const removeSession = useStopAndDeleteConversation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const navigate = useNavigate();
  const openInPanel = useContext(SubagentPanelOpenerContext);
  const queryClient = useQueryClient();
  const search = sessionNavigationSearch(useLocation().search);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const label = peer.title || peer.session_name || "Session";
  const harness = harnessAgentForChild(peer);
  async function save(action: () => Promise<unknown>) {
    setSaving(true);
    setError(null);
    try {
      await action();
      await queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(rootSessionId) });
      await queryClient.invalidateQueries({ queryKey: ["session", peer.id] });
      setRenaming(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update the session.");
    } finally {
      setSaving(false);
    }
  }
  return (
    <>
      <li
        className="group/peer mt-2 rounded-md border border-border/60"
        data-peer-session-id={peer.id}
      >
        <div className="flex items-start">
          <Link
            to={{ pathname: `/c/${peer.id}`, search }}
            data-testid="peer-session-row"
            className={cn(
              "min-w-0 flex-1 rounded-md px-2.5 py-2 hover:bg-muted",
              conversationId === peer.id && "bg-accent",
            )}
          >
            <div className="flex items-center gap-2">
              <StatusAvatar {...childStatus(peer)} />
              {harness && <ComposerAgentIcon agent={harness} className="size-4 shrink-0" />}
              <span className="truncate text-sm font-semibold">{label}</span>
            </div>
            {peer.last_message_preview && (
              <p className="mt-1 truncate text-xs text-muted-foreground">
                {peer.last_message_preview}
              </p>
            )}
          </Link>
          {openInPanel && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="mr-1 mt-2 shrink-0"
              aria-label={`Open ${label} in side panel`}
              title="Open in side panel"
              onClick={() => openInPanel(peer.id)}
            >
              <PanelRightOpenIcon className="size-3.5" />
            </Button>
          )}
        </div>
        {canEdit && (
          <div className="flex items-center gap-0.5 border-t border-border/50 px-2 py-1">
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Rename ${label}`}
              title="Rename session"
              onClick={() => {
                setName(label);
                setRenaming(true);
              }}
            >
              <PencilIcon className="size-3" />
            </Button>
            {!peer.archived && (
              <>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Move ${label} up`}
                  title="Move up"
                  disabled={!onMoveUp}
                  onClick={onMoveUp}
                >
                  <ArrowUpIcon className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Move ${label} down`}
                  title="Move down"
                  disabled={!onMoveDown}
                  onClick={onMoveDown}
                >
                  <ArrowDownIcon className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Archive ${label}`}
                  title="Archive session and its subagents"
                  disabled={archive.isPending}
                  onClick={() => {
                    if (conversationId === peer.id) navigate(`/c/${rootSessionId}`);
                    archive.mutate({ conversation: { id: peer.id }, archived: true });
                  }}
                >
                  <ArchiveIcon className="size-3" />
                </Button>
              </>
            )}
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Delete ${label}`}
              title="Delete session and its subagents"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2Icon className="size-3" />
            </Button>
            {peer.archived && (
              <Button
                variant="ghost"
                size="sm"
                disabled={archive.isPending}
                onClick={() => archive.mutate({ conversation: { id: peer.id }, archived: false })}
              >
                Unarchive
              </Button>
            )}
          </div>
        )}
        {renaming && (
          <form
            className="flex gap-1 p-2"
            onSubmit={(event) => {
              event.preventDefault();
              void save(() => renameConversation(peer.id, name.trim()));
            }}
          >
            <input
              aria-label="Session name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="min-w-0 flex-1 rounded border bg-background px-2 text-sm"
              autoFocus
            />
            <Button size="sm" type="submit" disabled={saving || !name.trim()}>
              Save
            </Button>
            <Button size="sm" variant="ghost" type="button" onClick={() => setRenaming(false)}>
              Cancel
            </Button>
          </form>
        )}
        {error && (
          <p role="alert" className="px-2 pb-2 text-xs text-destructive">
            {error}
          </p>
        )}
      </li>
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {label}?</DialogTitle>
            <DialogDescription>
              This session, its subagents, and their history will be deleted. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              disabled={removeSession.isPending}
              onClick={() => setDeleteOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={removeSession.isPending}
              onClick={() => {
                if (
                  conversationId === peer.id ||
                  cachedTreeContains(queryClient, peer.id, conversationId, MAX_TREE_DEPTH)
                )
                  navigate(`/c/${rootSessionId}`);
                removeSession.mutate(
                  { id: peer.id, deleteBranch: false },
                  {
                    onSuccess: () => setDeleteOpen(false),
                  },
                );
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {!peer.archived && (
        <HelperSection
          sessionId={peer.id}
          label={label}
          sessions={children.filter((child) => !child.archived)}
          conversationId={conversationId}
          collapsedRows={collapsedRows}
          onToggleCollapsed={onToggleCollapsed}
        />
      )}
    </>
  );
}

function HelperSection({
  sessionId,
  label,
  sessions,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
}: {
  sessionId: string;
  label: string;
  sessions: ChildSessionInfo[];
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
}) {
  const collapsed = collapsedRows[sessionId] ?? false;
  const working = sessions.filter((child) => childStatus(child).activity === "working").length;
  const awaiting = sessions.filter((child) => childStatus(child).activity === "awaiting").length;
  return (
    <>
      <li>
        <button
          type="button"
          className="flex w-full items-center gap-1 px-3 py-2 text-xs text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={`${collapsed ? "Expand" : "Collapse"} ${label} subagents`}
          aria-expanded={!collapsed}
          onClick={() => onToggleCollapsed(sessionId)}
        >
          {collapsed ? (
            <ChevronRightIcon className="size-3" />
          ) : (
            <ChevronDownIcon className="size-3" />
          )}
          <span>Subagents ({sessions.length})</span>
          {working > 0 && <span className="ml-auto">{working} working</span>}
          {awaiting > 0 && <span className="text-warning">{awaiting} needs input</span>}
        </button>
      </li>
      {!collapsed &&
        sessions.map((child, index) => (
          <SubagentRow
            key={child.id}
            child={child}
            depth={1}
            rootGuideContinues={index < sessions.length - 1}
            conversationId={conversationId}
            collapsedRows={collapsedRows}
            onToggleCollapsed={onToggleCollapsed}
          />
        ))}
    </>
  );
}

function ViewModeToggle({
  viewMode,
  onViewModeChange,
}: {
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
}) {
  return (
    <div className="flex h-11 shrink-0 items-center gap-0.5 border-b px-2">
      <h2 className="pl-1 font-medium text-ui">Agents</h2>
      <div className="ml-auto flex items-center gap-0.5">
        <Button
          variant={viewMode === "list" ? "secondary" : "ghost"}
          size="icon-xs"
          onClick={() => onViewModeChange("list")}
          aria-label="List view"
          title="List view"
          data-testid="view-mode-list"
        >
          <ListIcon className="size-3.5" />
        </Button>
        <Button
          variant={viewMode === "graph" ? "secondary" : "ghost"}
          size="icon-xs"
          onClick={() => onViewModeChange("graph")}
          aria-label="Graph view"
          title="Graph view"
          data-testid="view-mode-graph"
        >
          <NetworkIcon className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

// Settled states are de-emphasized (dimmed) so live agents dominate the list.
// Working is not dimmed — an actively-working agent should stay full-strength.
const SETTLED_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: false,
  awaiting: false,
  failed: false,
  // Not dimmed — a disconnected runner is something the user may want to
  // notice and act on (retry/reconnect), so it stays full-strength.
  disconnected: false,
  other: false,
  done: true,
  idle: true,
};

/**
 * Map a sub-agent type label to a category icon so a mix of agents reads by
 * role at a glance (Claude Code spawns many same-type "Explore" agents — the
 * icon distinguishes roles; the preview line below distinguishes instances).
 * Category icons are monochrome — the row applies the muted color; the
 * fallback is the generic bot icon.
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
  return BotIcon;
}

/**
 * Pick the harness identity for coding child sessions when the summary
 * carries enough metadata. The shared harness-selector icon component uses
 * this identity to render the same color variant in the Agents panel.
 *
 * Only full native sessions get the harness glyph. *Sub-agent* wrapper
 * children (``…-subagent``) deliberately fall through to the role icons
 * (and the generic bot fallback) — a native session's sub-agents are all the
 * same brand, so repeating the logo down the tree says nothing, while
 * role icons distinguish what each one is doing.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The agent identity used by the harness selector, or ``null``.
 */
function harnessAgentForChild(
  child: ChildSessionInfo,
): { name: string; harness: string | null } | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  if (nativeAgent) {
    return { name: nativeAgent.agentName, harness: nativeAgent.harness };
  }
  // Exact match — substring checks would false-match names like "pipeline".
  if (child.tool === PI_AGENT_NAME) {
    return { name: "pi-native-ui", harness: "pi-native" };
  }
  return null;
}

/**
 * Resolve the semantic role that chooses a sub-agent's category icon.
 *
 * Native runtimes may give an instance a friendly nickname (for example,
 * Codex's "Archimedes") while carrying its functional role separately. The
 * nickname remains the row label; the role drives iconography.
 */
function iconRoleForChild(child: ChildSessionInfo): string | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  if (wrapper === CODEX_NATIVE_SUBAGENT_WRAPPER) {
    return child.labels?.[CODEX_NATIVE_SUBAGENT_ROLE_LABEL] ?? child.tool;
  }
  if (wrapper === ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER) {
    return child.labels?.[ANTIGRAVITY_NATIVE_SUBAGENT_ROLE_LABEL] ?? child.tool;
  }
  return child.tool;
}

const STATUS_AVATAR_CLASS: Record<AgentActivity, string> = {
  launching: "bg-muted text-muted-foreground",
  working: "bg-muted text-muted-foreground",
  awaiting: "bg-warning/15 text-warning",
  failed: "bg-destructive/10 text-destructive",
  disconnected: "bg-muted text-muted-foreground",
  other: "bg-muted text-muted-foreground",
  done: "bg-success/10 text-success",
  idle: "bg-muted text-muted-foreground",
};

const STATUS_AVATAR_ICON: Record<AgentActivity, AgentRowIcon | null> = {
  launching: null,
  working: null,
  awaiting: CircleHelpIcon,
  failed: CircleAlertIcon,
  disconnected: UnplugIcon,
  other: EllipsisIcon,
  done: CheckIcon,
  idle: PauseIcon,
};

/** Compact state tile shown before each agent avatar. */
function StatusAvatar({ activity, label, details }: AgentStatus) {
  const title = details ? `${label}: ${details}` : label;
  const Icon = STATUS_AVATAR_ICON[activity];
  return (
    <span
      aria-label={title}
      title={title}
      data-testid="subagent-status-avatar"
      data-activity={activity}
      className={cn(
        "flex size-6 shrink-0 items-center justify-center rounded-md",
        STATUS_AVATAR_CLASS[activity],
      )}
    >
      {Icon ? (
        <Icon aria-hidden="true" className="size-3.5" />
      ) : (
        <RunningDot className="size-3.5 text-current" />
      )}
    </span>
  );
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

function MainRow({
  rootSessionId,
  isActive,
  fullSession = false,
}: {
  rootSessionId: string;
  isActive: boolean;
  fullSession?: boolean;
}) {
  const openInPanel = useContext(SubagentPanelOpenerContext);
  const { session } = useSession(rootSessionId);
  const search = sessionNavigationSearch(useLocation().search);
  // Same wrapper-label probe used by the sidebar (Sidebar.tsx) and
  // TerminalFirstContext to decide a session is claude/codex-native.
  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  const isNessie = session?.agentName === "nessie";
  // Native wrappers show the product name (mirroring the sidebar) instead
  // of the spec's YAML name (e.g. "claude-native-ui"); other agents show
  // their agent name, with "main" only while the session loads or when it
  // carries no name.
  const label = nativeAgent?.displayName ?? session?.agentName ?? "main";
  const preview = mainMessagePreview(session?.items);
  return (
    <li className="relative">
      {!fullSession && (
        <span
          aria-hidden="true"
          style={{ left: ROOT_GUIDE_CENTER_PX }}
          className="pointer-events-none absolute bottom-0 top-10 z-10 border-l border-dashed border-border/70"
        />
      )}
      {fullSession && openInPanel && (
        <Button
          variant="ghost"
          size="icon-xs"
          className="absolute right-1 top-2 z-10"
          aria-label="Open Main in side panel"
          title="Open in side panel"
          onClick={() => openInPanel(rootSessionId)}
        >
          <PanelRightOpenIcon className="size-3.5" />
        </Button>
      )}
      <Link
        // Drop session-scoped params (``file``, ``diff``, ``comment``,
        // ``view``, ``message``) when navigating in the rail — those are tied to
        // one session's file-viewer state and must not bleed into the
        // next. Global params like ``?debug=1`` are preserved by
        // ``sessionNavigationSearch`` so debug mode stays on across navigation.
        to={{ pathname: `/c/${rootSessionId}`, search }}
        data-testid="subagent-main-row"
        data-root-session-id={rootSessionId}
        data-agent-kind={
          nativeAgent != null ? `${nativeAgent.key}-native` : isNessie ? "nessie" : "agent"
        }
        className={cn(
          "flex w-full flex-col gap-0.5 rounded-md px-2.5 py-2 text-left",
          isActive ? "bg-accent" : "hover:bg-muted",
        )}
      >
        <div className="flex w-full items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-[2px]">
              {fullSession && session && (
                <StatusAvatar
                  {...((session.pendingElicitations?.length ?? 0) > 0
                    ? { activity: "awaiting", label: "Needs input" }
                    : sessionStatus(session.status, session.lastTaskError))}
                />
              )}
              <span
                data-testid="subagent-main-harness-icon"
                className="flex size-8 shrink-0 items-center justify-center rounded-xl border border-border bg-background"
              >
                <ComposerAgentIcon
                  agent={{
                    name: session?.agentName ?? nativeAgent?.agentName ?? "",
                    harness: session?.harness ?? nativeAgent?.harness ?? null,
                  }}
                  className="size-5"
                />
              </span>
              <span className="min-w-0 truncate text-base font-semibold">
                {fullSession ? "Main" : label}
              </span>
              {fullSession && (
                <span className="ml-1 truncate text-xs text-muted-foreground">{label}</span>
              )}
            </div>
            {preview && (
              <p
                data-testid="subagent-main-preview"
                className="truncate text-sm text-muted-foreground"
              >
                {preview}
              </p>
            )}
          </div>
        </div>
      </Link>
    </li>
  );
}

const ROOT_GUIDE_CENTER_PX = 26;

// The root harness avatar's center sits at 26px (10px row inset + 16px).
// Center direct children on that guide. Align the nested connector container
// with its parent's title container, then preserve that step at deeper levels.
const ROW_BASE_PADDING_PX = 14;
const ROW_DEPTH_STEP_PX = 90;
const SUBAGENT_ROW_OFFSET_PX = 26;
const ROW_VERTICAL_PADDING_PX = 4;
const ROW_CONNECTOR_SIZE_PX = 24;

function rowPaddingLeft(depth: number): number {
  return (
    ROW_BASE_PADDING_PX + (depth - 1) * ROW_DEPTH_STEP_PX - (depth > 1 ? SUBAGENT_ROW_OFFSET_PX : 0)
  );
}

function SubagentRow({
  child,
  depth,
  rootGuideContinues,
  conversationId,
  collapsedRows,
  onToggleCollapsed,
}: {
  child: ChildSessionInfo;
  /** Levels below the root, 1 = direct child of "main". */
  depth: number;
  /** Whether the root guide must continue to a later first-level sibling. */
  rootGuideContinues: boolean;
  /** The conversation currently rendered in main, for row highlighting. */
  conversationId: string;
  collapsedRows: Record<string, boolean>;
  onToggleCollapsed: (id: string) => void;
}) {
  const collapsed = collapsedRows[child.id] ?? false;
  const status = childStatus(child);
  const search = sessionNavigationSearch(useLocation().search);
  const harnessAgent = harnessAgentForChild(child);
  const RoleIcon = iconForAgentType(iconRoleForChild(child));
  const primary = childPrimaryLabel(child);
  const isActive = conversationId === child.id;
  const openInPanel = useOpenSubagentInPanel();
  // De-emphasize settled rows (done/idle) so working/failed agents dominate
  // — but never the row the user is currently viewing.
  const dim = !isActive && SETTLED_STATE[status.activity];
  // This child's own sub-agents, rendered as the next tree level.
  // Disabled (null id) at the depth cap so the fan-out of fetches is
  // bounded; ``useChildSessions`` skips the query entirely for null.
  const { children: descendants } = useChildSessions(depth < MAX_TREE_DEPTH ? child.id : null);
  const grandchildren = descendants.filter((descendant) => !descendant.archived);
  const hasGrandchildren = grandchildren.length > 0;
  const ToggleIcon = collapsed ? ChevronRightIcon : ChevronDownIcon;
  return (
    <>
      <li className="group/agent relative">
        {depth === 1 ? (
          <>
            <span
              aria-hidden="true"
              data-root-guide-segment="upper"
              style={{ left: ROOT_GUIDE_CENTER_PX, height: ROW_VERTICAL_PADDING_PX }}
              className="pointer-events-none absolute top-0 border-l border-dashed border-border/70"
            />
            {rootGuideContinues && (
              <span
                aria-hidden="true"
                data-root-guide-segment="lower"
                style={{
                  left: ROOT_GUIDE_CENTER_PX,
                  top: ROW_VERTICAL_PADDING_PX + ROW_CONNECTOR_SIZE_PX,
                }}
                className="pointer-events-none absolute bottom-0 border-l border-dashed border-border/70"
              />
            )}
          </>
        ) : rootGuideContinues ? (
          <span
            aria-hidden="true"
            data-root-guide-segment="continuation"
            style={{ left: ROOT_GUIDE_CENTER_PX }}
            className="pointer-events-none absolute inset-y-0 border-l border-dashed border-border/70"
          />
        ) : null}
        {hasGrandchildren && (
          <button
            type="button"
            data-testid="subagent-collapse-toggle"
            aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand subagents" : "Collapse subagents"}
            style={{ left: rowPaddingLeft(depth) }}
            className="absolute top-1 z-10 flex size-6 items-center justify-center rounded-md bg-transparent text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            onClick={(event) => {
              event.stopPropagation();
              onToggleCollapsed(child.id);
            }}
          >
            <ToggleIcon aria-hidden="true" className="size-3.5" />
          </button>
        )}
        <Link
          // See MainRow: drop session-scoped params on rail navigation
          // (preserving global ones like ``?debug=1``) so a sticky
          // ``?file=`` from the previous session doesn't carry over.
          to={{ pathname: `/c/${child.id}`, search }}
          data-testid="subagent-row"
          data-child-session-id={child.id}
          onClick={(event) => {
            openInPanel(child.id, event);
          }}
          data-depth={depth}
          // Left gutter (depth-stepped) + connector glyph nests this row
          // under its parent, signaling where it sits in the tree.
          style={{ paddingLeft: rowPaddingLeft(depth) }}
          className={cn(
            "flex w-full flex-col gap-0.5 rounded-md py-1 pr-1 text-left",
            isActive ? "bg-accent" : "group-hover/agent:bg-muted",
            dim && "opacity-60 group-hover/agent:opacity-100",
          )}
        >
          <div className="flex w-full items-start gap-2">
            {hasGrandchildren ? (
              <span aria-hidden="true" className="size-6 shrink-0" />
            ) : (
              <span
                aria-hidden="true"
                className="flex size-6 shrink-0 items-center justify-center rounded-md bg-transparent"
              >
                {depth === 1 ? (
                  <CircleDotIcon
                    data-subagent-connector
                    className="size-4 text-muted-foreground/60"
                  />
                ) : (
                  <CornerDownRightIcon
                    // Decorative nesting connector — the role icon beside it carries
                    // the meaning, so hide this from the accessibility tree.
                    data-subagent-connector
                    className="size-4 text-muted-foreground/60"
                  />
                )}
              </span>
            )}
            <StatusAvatar {...status} />
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-[2px]">
                <span
                  data-testid="subagent-agent-avatar"
                  className="flex size-6 shrink-0 items-center justify-center"
                >
                  {harnessAgent ? (
                    <ComposerAgentIcon agent={harnessAgent} className="size-4" />
                  ) : (
                    <RoleIcon className="size-4 text-muted-foreground" />
                  )}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm font-medium">{primary}</span>
                {child.routed_model ? (
                  // Model the intelligent router picked for this sub-agent — the
                  // per-subagent half of routing visibility.
                  <span
                    data-testid="subagent-routed-model"
                    title={`Smart routing picked ${child.routed_model}`}
                    className="shrink-0 truncate font-mono text-[10px] text-muted-foreground"
                  >
                    {shortModelName(child.routed_model)}
                  </span>
                ) : null}
              </div>
              {child.last_message_preview && (
                <p className="mt-0.5 truncate text-sm text-muted-foreground">
                  {child.last_message_preview}
                </p>
              )}
            </div>
          </div>
        </Link>
      </li>
      {!collapsed &&
        grandchildren.map((grandchild) => (
          <SubagentRow
            key={grandchild.id}
            child={grandchild}
            depth={depth + 1}
            rootGuideContinues={rootGuideContinues}
            conversationId={conversationId}
            collapsedRows={collapsedRows}
            onToggleCollapsed={onToggleCollapsed}
          />
        ))}
    </>
  );
}
