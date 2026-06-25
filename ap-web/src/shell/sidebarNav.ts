import type { Conversation } from "@/hooks/useConversations";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

export const PINNED_CONVERSATION_IDS_STORAGE_KEY = "omnigent:pinned-conversation-ids";

// Titles of sidebar sections the user has collapsed, e.g. ["Archived"].
// Keyed by display title — stable identifiers for these fixed groups.
export const COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY = "omnigent:collapsed-sidebar-sections";

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
  | "goose"
  | "antigravity"
  | "qwen"
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
