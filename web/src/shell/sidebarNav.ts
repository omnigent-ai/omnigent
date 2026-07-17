import type { Conversation } from "@/hooks/useConversations";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

export const PINNED_CONVERSATION_IDS_STORAGE_KEY = "omnigent:pinned-conversation-ids";

// Titles of sidebar sections the user has collapsed, e.g. ["Archived"].
// Keyed by display title — stable identifiers for these fixed groups.
export const COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY = "omnigent:collapsed-sidebar-sections";

// Names of project folders the user has expanded. Project folders default to
// COLLAPSED (so the sidebar stays short as project count grows), so this is
// the inverse of the fixed-section collapse set: a project shows its rows only
// when its name is present here.
export const EXPANDED_PROJECT_SECTIONS_STORAGE_KEY = "omnigent:expanded-project-sections";

// Snapshot of the active chat's updated_at at the moment the user
// entered it. Used as the sort key for the active row so subsequent
// updated_at bumps (the user sending a message) don't move it.
export interface ActiveChatOverride {
  id: string;
  updatedAt: number;
}

// Exported so other surfaces (e.g. the Agents rail's main row) show the
// same friendly product names for native-wrapper sessions.
export const CLAUDE_NATIVE_DEFAULT_LABEL = "Claude Code";
export const CODEX_NATIVE_DEFAULT_LABEL = "Codex";
export const PI_NATIVE_DEFAULT_LABEL = "Pi";

export type ConversationIconKind =
  | "claude"
  | "codex"
  | "opencode"
  | "pi"
  | "cursor"
  | "kiro"
  | "goose"
  | "antigravity"
  | "qwen"
  | "kimi"
  | "hermes"
  | "nessie"
  | null;

// Display label for a session with no title and no native-wrapper name —
// shown in the sidebar row and as the browser tab title fallback.
export const UNTITLED_CONVERSATION_LABEL = "New session";

function wrapperLabel(conversation: Conversation): string | undefined {
  return conversation.labels?.[WRAPPER_LABEL_KEY];
}

function nativeWrapperLabel(conversation: Conversation): string | null {
  const wrapper = wrapperLabel(conversation);
  return nativeCodingAgentForWrapper(wrapper)?.displayName ?? null;
}

export function getConversationIconKind(conversation: Conversation): ConversationIconKind {
  const wrapper = wrapperLabel(conversation);
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  if (nativeAgent != null) return nativeAgent.iconKind;
  if (conversation.agent_name === "nessie") return "nessie";
  return null;
}

export function getConversationAgentType(conversation: Conversation): string {
  const label = nativeWrapperLabel(conversation);
  if (label !== null) return label;
  if (conversation.agent_name) {
    return conversation.agent_name;
  }
  return "Other";
}

export function conversationDisplayLabel(conversation: Conversation): string {
  if (conversation.title) return conversation.title;
  const label = nativeWrapperLabel(conversation);
  if (label !== null) return label;
  return UNTITLED_CONVERSATION_LABEL;
}

export function filterConversations(
  conversations: Conversation[],
  searchQuery: string,
): Conversation[] {
  const query = searchQuery.trim().toLocaleLowerCase();
  if (!query) return conversations;

  return conversations.filter((conversation) => {
    const display = conversationDisplayLabel(conversation).toLocaleLowerCase();
    const id = conversation.id.toLocaleLowerCase();
    return display.includes(query) || id.includes(query);
  });
}

// Sort by `updated_at` desc so the order matches the row's relative-time
// pill. The active chat uses its frozen snapshot from
// `activeOverride` instead of its live `updated_at`, so sending a message
// in the chat you're already viewing doesn't move it.
export function sortByUpdatedAtDesc(
  conversations: Conversation[],
  activeOverride: ActiveChatOverride | null,
): Conversation[] {
  const effective = (c: Conversation): number =>
    activeOverride?.id === c.id ? activeOverride.updatedAt : c.updated_at;
  return [...conversations].sort((a, b) => effective(b) - effective(a));
}

// Decide the next `activeOverride` value given the current route and
// loaded conversations. Pulled out so the freeze behavior can be
// unit-tested without driving a React render.
export function computeNextActiveOverride(
  activeId: string | undefined,
  conversations: readonly Conversation[],
  previous: ActiveChatOverride | null,
): ActiveChatOverride | null {
  if (!activeId) return null;
  // Already frozen for this chat — return the same reference so callers
  // can use reference equality to skip a state update.
  if (previous?.id === activeId) return previous;
  const active = conversations.find((c) => c.id === activeId);
  // Active id is set but the conversation hasn't loaded into the page
  // yet. Drop any prior override (we've left that chat) and wait — the
  // effect will re-run once the list arrives.
  if (!active) return null;
  return { id: activeId, updatedAt: active.updated_at };
}

export function togglePinnedConversationId(
  pinnedIds: readonly string[],
  conversationId: string,
): string[] {
  if (pinnedIds.includes(conversationId)) {
    return pinnedIds.filter((id) => id !== conversationId);
  }
  return [conversationId, ...pinnedIds];
}

// A bare 32-char lowercase-hex conversation id — the shape the API returns now
// that ids are stored as 16-byte binary uuids (legacy prefixes dropped).
const BARE_CONVERSATION_ID_RE = /^[0-9a-f]{32}$/i;

// Reduce a possibly-legacy conversation id to the bare hex form the server now
// emits: drop dashes and any legacy prefix (``conv_``/``ag_``/…) and keep the
// trailing 32 hex chars, mirroring the id-to-binary DB migration's transform.
// A value that isn't a uuid tail (hand-crafted junk) is returned unchanged.
export function bareConversationId(id: string): string {
  const tail = id.replace(/-/g, "").slice(-32);
  return BARE_CONVERSATION_ID_RE.test(tail) ? tail.toLowerCase() : id;
}

// Migrate stored pin ids to the bare-hex form, dropping duplicates that collapse
// together (a legacy ``conv_<hex>`` and its bare twin). Pins are persisted in
// localStorage keyed by the conversation id, so ids pinned before the migration
// still carry the old prefix. Without this, a returning user's pins no longer
// match the ids the API returns: the pinned session loses its Pinned slot, and
// the pinned-backfill re-fetches it under the bare id — surfacing a duplicate
// row alongside the copy already in the loaded list.
export function migratePinnedConversationIds(ids: readonly string[]): string[] {
  const seen = new Set<string>();
  const migrated: string[] = [];
  for (const id of ids) {
    const bare = bareConversationId(id);
    if (seen.has(bare)) continue;
    seen.add(bare);
    migrated.push(bare);
  }
  return migrated;
}

// Drop conversations whose id already appeared, keeping the first occurrence.
// The pinned-backfill fetches a session by id and can return a copy that is
// also present in the paginated list; merging both would render the row twice.
export function dedupeConversationsById(conversations: readonly Conversation[]): Conversation[] {
  const seen = new Set<string>();
  const deduped: Conversation[] = [];
  for (const conversation of conversations) {
    if (seen.has(conversation.id)) continue;
    seen.add(conversation.id);
    deduped.push(conversation);
  }
  return deduped;
}

// Order pinned conversations by when they were pinned, not by `updated_at` —
// a pinned session holds its slot even when a new message bumps its
// `updated_at`. `pinnedIds` is kept most-recently-pinned-first (see
// `togglePinnedConversationId`), so we reverse it: the oldest pin ranks
// first (top) and a freshly pinned session lands at the bottom of the group.
// Anything not in `pinnedIds` (shouldn't happen for this list) sinks to the
// bottom in a stable order.
export function orderByPinnedSequence(
  conversations: Conversation[],
  pinnedIds: readonly string[],
): Conversation[] {
  const oldestPinFirst = [...pinnedIds].reverse();
  const rankById = new Map(oldestPinFirst.map((id, index) => [id, index]));
  const rank = (c: Conversation): number => rankById.get(c.id) ?? Number.MAX_SAFE_INTEGER;
  return [...conversations].sort((a, b) => rank(a) - rank(b));
}

export function normalizePinnedConversationIds(
  pinnedIds: readonly string[],
  conversations: readonly Conversation[],
): string[] {
  const validIds = new Set(conversations.map((conversation) => conversation.id));
  const seen = new Set<string>();
  const normalized: string[] = [];

  for (const id of pinnedIds) {
    if (!validIds.has(id) || seen.has(id)) continue;
    seen.add(id);
    normalized.push(id);
  }

  return normalized;
}

// ── Drag-and-drop ────────────────────────────────────────────────────────────

/** The session being dragged: its id, the project it's currently filed under
    (`null` when it lives in the flat list, outside any project), and whether
    it's already pinned. */
export interface SidebarDragSource {
  id: string;
  project: string | null;
  isPinned: boolean;
}

/** What a row was dropped onto. A project folder files the session into that
    project; the "ungroup" zone removes it from its project; the "pin" zone
    pins it (which moves it out of its project via pin-precedence). `null` is a
    drop that landed on nothing droppable (e.g. "Shared with me", which is
    never a target — sessions can't be filed there). */
export type SidebarDropTarget =
  | { type: "project"; name: string }
  | { type: "ungroup" }
  | { type: "pin" }
  | null;

/** The action a drop resolves to. `move` files the session into a project;
    `ungroup` removes it from its current project (the caller still confirms
    when it's the project's last member); `pin` pins it (pin-precedence then
    floats it into the Pinned section); `unpin` just unpins it (so it leaves
    Pinned and falls back to its project / the flat list); `none` is a no-op.

    `move`/`ungroup` carry an `unpin` flag: a PINNED session is shown in the
    Pinned section regardless of its project label, so moving/unfiling it has no
    visible effect until it's also unpinned. Dragging a pinned row onto a
    project / Chats therefore unpins it too, so it actually lands where dropped
    (this is why a pinned session previously appeared "stuck" in Pinned). */
export type SidebarDropAction =
  | { kind: "move"; project: string; unpin: boolean }
  | { kind: "ungroup"; project: string; unpin: boolean }
  | { kind: "pin" }
  | { kind: "unpin" }
  | { kind: "none" };

/**
 * Pure resolution of a sidebar drag-and-drop: given the dragged session and the
 * target it was released over, decide whether to file it into a project, remove
 * it from its project, pin/unpin it, or do nothing. Kept side-effect-free so the
 * routing is unit-testable independent of dnd-kit and the mutation hooks.
 *
 * - Dropped on a project folder it isn't already in → `move` (+`unpin` if pinned).
 * - Dropped on its OWN folder → `none`, unless pinned (then `move` to re-reveal
 *   it in that folder by unpinning — no visible change otherwise).
 * - Dropped on the ungroup zone while filed → `ungroup` (+`unpin` if pinned).
 * - Dropped on the ungroup zone while unfiled → `unpin` if pinned, else `none`.
 * - Dropped on the pin zone while not already pinned → `pin`.
 * - Dropped on the pin zone while already pinned → `none`.
 * - Dropped on nothing → `none`.
 */
export function resolveSidebarDrop(
  source: SidebarDragSource,
  target: SidebarDropTarget,
): SidebarDropAction {
  if (!target) return { kind: "none" };
  if (target.type === "project") {
    // Same project, not pinned → nothing to do. Same project but pinned → the
    // session is hidden up in Pinned, so re-file it (a no-op label write) and
    // unpin so it drops into this folder.
    if (target.name === source.project && !source.isPinned) return { kind: "none" };
    return { kind: "move", project: target.name, unpin: source.isPinned };
  }
  if (target.type === "pin") {
    // Pinning an already-pinned session is a no-op; otherwise pin it (the list
    // floats pinned sessions out of their project into the Pinned section).
    return source.isPinned ? { kind: "none" } : { kind: "pin" };
  }
  // Ungroup (dropped on "Chats" / the fallback strip): land it in the flat list.
  if (source.project) return { kind: "ungroup", project: source.project, unpin: source.isPinned };
  // No project label: only meaningful if pinned (unpin → it drops into Chats).
  return source.isPinned ? { kind: "unpin" } : { kind: "none" };
}
