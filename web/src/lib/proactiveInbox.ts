// Agent-posted Inbox suggestions (`/v1/inbox/items`): wire types, the two API
// calls, and pure helpers the Inbox page renders from. Kept free of React so
// the mapping and link checks are unit-testable.
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type SuggestionKind = "followup" | "info" | "reminder";
export type SuggestionState = "unread" | "read" | "done" | "dismissed";
export type SuggestionFeedback = "none" | "useful" | "not_useful";

export interface SuggestionLink {
  type: "session" | "pr" | "url";
  ref: string;
  label?: string;
}

/** One suggestion an agent posted to the viewer's Inbox. Times are epoch seconds. */
export interface Suggestion {
  id: string;
  kind: SuggestionKind;
  title: string;
  bodyMd: string;
  links: SuggestionLink[];
  suggestedPrompt: string | null;
  sourceSessionId: string;
  state: SuggestionState;
  feedback: SuggestionFeedback;
  createdAt: number;
}

interface SuggestionWire {
  id: string;
  kind: SuggestionKind;
  title: string;
  body_md: string;
  links: SuggestionLink[];
  suggested_prompt: string | null;
  source_session_id: string;
  state: SuggestionState;
  feedback: SuggestionFeedback;
  created_at: number;
}

export interface SuggestionPage {
  data: Suggestion[];
  unreadCount: number;
}

/** States still waiting on the viewer; done and dismissed items leave the Inbox. */
const OPEN_STATES: SuggestionState[] = ["unread", "read"];
export const SUGGESTION_PAGE_SIZE = 50;

export function suggestionFromWire(wire: SuggestionWire): Suggestion {
  return {
    id: wire.id,
    kind: wire.kind,
    title: wire.title,
    bodyMd: wire.body_md,
    links: wire.links ?? [],
    suggestedPrompt: wire.suggested_prompt,
    sourceSessionId: wire.source_session_id,
    state: wire.state,
    feedback: wire.feedback,
    createdAt: wire.created_at,
  };
}

/** The viewer's open suggestions, newest first, with the server's unread count. */
export async function listSuggestions(): Promise<SuggestionPage> {
  const params = new URLSearchParams({ limit: String(SUGGESTION_PAGE_SIZE) });
  for (const state of OPEN_STATES) params.append("state", state);
  const res = await authenticatedFetch(`/v1/inbox/items?${params}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const page = (await res.json()) as { data: SuggestionWire[]; unread_count: number };
  return { data: page.data.map(suggestionFromWire), unreadCount: page.unread_count };
}

export interface SuggestionUpdate {
  state?: SuggestionState;
  feedback?: SuggestionFeedback;
}

/** Record the viewer's response to one suggestion. */
export async function updateSuggestion(id: string, update: SuggestionUpdate): Promise<Suggestion> {
  const res = await authenticatedFetch(`/v1/inbox/items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return suggestionFromWire((await res.json()) as SuggestionWire);
}

/** Apply an update to a cached page, adjusting the unread count to match. */
export function applySuggestionUpdate(
  page: SuggestionPage,
  id: string,
  update: SuggestionUpdate,
): SuggestionPage {
  let unreadCount = page.unreadCount;
  const data = page.data.flatMap((item) => {
    if (item.id !== id) return [item];
    const next = { ...item, ...update };
    if (item.state === "unread" && next.state !== "unread") unreadCount -= 1;
    return OPEN_STATES.includes(next.state) ? [next] : [];
  });
  return { data, unreadCount: Math.max(0, unreadCount) };
}

/** Where a link goes: a session in this app, an external http(s) page, or nowhere. */
export type SuggestionLinkTarget =
  { kind: "session"; path: string } | { kind: "external"; href: string } | null;

export function suggestionLinkTarget(link: SuggestionLink): SuggestionLinkTarget {
  const ref = link.ref.trim();
  if (!ref) return null;
  if (link.type === "session") return { kind: "session", path: `/c/${encodeURIComponent(ref)}` };
  // Agents write these refs, so only plain web links are rendered as links.
  try {
    const url = new URL(ref.includes("://") ? ref : `https://${ref}`);
    return url.protocol === "https:" || url.protocol === "http:"
      ? { kind: "external", href: url.toString() }
      : null;
  } catch {
    return null;
  }
}

export function suggestionKindLabel(kind: SuggestionKind): string {
  if (kind === "followup") return "Suggested follow-up";
  if (kind === "reminder") return "Reminder";
  return "For your information";
}

/** The first line of a markdown body, stripped of leading markdown markers. */
export function suggestionPreview(bodyMd: string, maxChars = 200): string {
  const line = bodyMd
    .split("\n")
    .map((part) => part.replace(/^\s*(?:[#>*-]+|\d+\.)\s*/, "").trim())
    .find(Boolean);
  if (!line) return "";
  return line.length > maxChars ? `${line.slice(0, maxChars - 1)}…` : line;
}
