import type { Conversation } from "@/hooks/useConversations";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { getOptimisticTitle } from "@/lib/optimisticTitles";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";

export const PINNED_CONVERSATION_IDS_STORAGE_KEY = "omnigent:pinned-conversation-ids";

// ── Legacy localStorage pin helpers ───────────────────────────────────────
//
// Pins are server-authoritative now, but two paths still touch the legacy
// localStorage key: the one-time server migration, and the pin toggle's
// fallback when the server can't yet store pins (`filterHonored === false` — a
// pre-upgrade server that ignores `?pinned=true`). Kept in this leaf module so
// both the Sidebar and the `useConversations` toggle hook can use them without
// an import cycle.

/** Read the legacy pin ids, migrated to bare-hex form. Corrupt/absent ⇒ []. */
export function readPinnedConversationIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Migrate legacy prefixed ids (``conv_<hex>``) to the bare-hex form the API
    // returns post id-to-binary migration; callers re-persist so the one-time
    // rewrite is durable across reloads.
    return migratePinnedConversationIds(
      parsed.filter((value): value is string => typeof value === "string"),
    );
  } catch {
    // Browser storage is user-editable and can contain stale/corrupt values.
    // Treat bad pin state as "no pins" instead of breaking navigation.
    return [];
  }
}

/** Remove the legacy pin key entirely (migration complete). */
export function clearLegacyPinnedConversationIds(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
  } catch {
    // Best-effort cleanup — a stale key is harmless (dedup + migration guard
    // skip already-known ids), so a failure here needn't surface.
  }
}

// Overwrite the legacy key with exactly `ids` (empty ⇒ remove). Throws if the
// browser rejects the write (e.g. storage quota exceeded); no window ⇒ no-op.
function writeLegacyPinnedConversationIdsOrThrow(ids: readonly string[]): void {
  if (typeof window === "undefined") return;
  if (ids.length === 0) {
    window.localStorage.removeItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
  } else {
    window.localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(ids));
  }
}

// Overwrite the legacy key with exactly `ids` (empty ⇒ remove). Best-effort:
// swallows write errors. Used by the migration to retain only the pins whose
// server write failed — a failure here just means the migration retries on the
// next load, so it must not surface.
export function writeLegacyPinnedConversationIds(ids: readonly string[]): void {
  try {
    writeLegacyPinnedConversationIdsOrThrow(ids);
  } catch {
    // Best-effort — a failed write is retried on the next load.
  }
}

/**
 * Add or remove a single id in the legacy localStorage pin list, most-recently-
 * pinned-first (matching the pre-server ordering the migration relies on).
 * Used by the pin toggle's old-server fallback so a pin created before the
 * server upgrade survives in localStorage and later migrates like any other
 * pre-upgrade pin.
 *
 * Throws if the write fails (unlike the migration's best-effort write): this is
 * the fallback's ONLY persistence, so a swallowed failure would let the toggle
 * report success while the pin silently vanishes on reload. Throwing rejects
 * the mutation instead, so its optimistic patch rolls back and the UI honestly
 * shows the pin didn't take — matching the server PATCH path's failure handling.
 */
export function setLegacyPinnedConversationId(id: string, pinned: boolean): void {
  const rest = readPinnedConversationIds().filter((x) => x !== id);
  writeLegacyPinnedConversationIdsOrThrow(pinned ? [id, ...rest] : rest);
}

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
  // Just-created session: its first prompt stands in until a real title lands.
  const optimistic = getOptimisticTitle(conversation.id);
  if (optimistic !== undefined) return optimistic;
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
//
// `frozenKeys` (when non-null) pins EVERY row's sort key at its
// first-seen value: a row's key is read from the map, or captured into it
// on first sight. The sidebar passes its map while the pointer is inside
// the list, so background `updated_at` bumps can't slide rows under the
// cursor — a mid-interaction reorder sends clicks, right-clicks, and the
// renames they trigger to the wrong session. Rows first seen while frozen
// (a folder expanding, a page loading) capture their key on entry, and
// the caller clears the map when the pointer leaves so the order snaps
// back to reality.
export function sortByUpdatedAtDesc(
  conversations: Conversation[],
  activeOverride: ActiveChatOverride | null,
  frozenKeys?: Map<string, number> | null,
): Conversation[] {
  const effective = (c: Conversation): number => {
    if (frozenKeys) {
      const frozen = frozenKeys.get(c.id);
      if (frozen !== undefined) return frozen;
      // Capture the override value for the active row so dropping the
      // override mid-hover (navigating to another chat) can't move it.
      const live = activeOverride?.id === c.id ? activeOverride.updatedAt : c.updated_at;
      frozenKeys.set(c.id, live);
      return live;
    }
    return activeOverride?.id === c.id ? activeOverride.updatedAt : c.updated_at;
  };
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
// together (a legacy ``conv_<hex>`` and its bare twin). Legacy pins were
// persisted in localStorage keyed by the conversation id, so ids pinned before
// the id-to-binary migration still carry the old prefix. Applied when reading
// those legacy pins for the one-time push up to server-side labels, so a
// returning user's pins map to the bare ids the API now returns.
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
// The server pinned list and the paginated list overlap (a pinned session is
// usually in both); merging them would otherwise render the row twice.
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
// `updated_at`. The `omnigent.pinned` label value is the epoch-ms pin time;
// sort ascending so the oldest pin ranks first (top) and a freshly pinned
// session lands at the bottom of the group (matching the prior localStorage
// behaviour). A missing/unparseable value sinks to the bottom, stably.
export function orderByPinnedTimestamp(conversations: readonly Conversation[]): Conversation[] {
  const pinnedAt = (c: Conversation): number => {
    const raw = c.labels?.[PINNED_LABEL_KEY];
    const ms = raw ? Number(raw) : NaN;
    return Number.isFinite(ms) ? ms : Number.MAX_SAFE_INTEGER;
  };
  return [...conversations].sort((a, b) => pinnedAt(a) - pinnedAt(b));
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
  { type: "project"; name: string } | { type: "ungroup" } | { type: "pin" } | null;

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
