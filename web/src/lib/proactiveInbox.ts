// Agent-posted Inbox suggestions (`/v1/inbox/items`): wire types, the API
// calls, and pure helpers the Inbox page renders from. Kept free of React so
// the mapping, cache updates, and link checks are unit-testable.
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type SuggestionKind = "followup" | "info" | "reminder";
export type SuggestionState = "unread" | "read" | "done" | "dismissed";
export type SuggestionResolution =
  "none" | "action_taken" | "already_done" | "useful" | "not_relevant" | "incorrect" | "not_now";

export interface SuggestionLink {
  type: "session" | "pr" | "url";
  ref: string;
  label?: string;
}

/** An agent-defined button that opens a new session prefilled with `prompt`. */
export interface SuggestionAction {
  label: string;
  prompt: string;
}

/** One suggestion an agent posted to the viewer's Inbox. Times are epoch seconds. */
export interface Suggestion {
  id: string;
  kind: SuggestionKind;
  title: string;
  bodyMd: string;
  links: SuggestionLink[];
  actions: SuggestionAction[];
  /** The agent's declarative chart, unvalidated; see `parseSuggestionChart`. */
  chart: unknown;
  sourceSessionId: string;
  state: SuggestionState;
  resolution: SuggestionResolution;
  createdAt: number;
}

interface SuggestionWire {
  id: string;
  kind: SuggestionKind;
  title: string;
  body_md: string;
  links?: SuggestionLink[];
  actions?: SuggestionAction[];
  chart?: unknown;
  source_session_id: string;
  state: SuggestionState;
  resolution?: SuggestionResolution;
  created_at: number;
}

export interface SuggestionPage {
  data: Suggestion[];
  nextCursor: string | null;
  unreadCount: number;
  /** The server stops counting at its limit; show the count as "N+". */
  unreadCountCapped: boolean;
}

/** States still waiting on the viewer; done and dismissed items leave the Inbox. */
export const OPEN_SUGGESTION_STATES: readonly SuggestionState[] = ["unread", "read"];
export const SUGGESTION_PAGE_SIZE = 50;

/** Reasons the Dismiss menu offers, in menu order. */
export const DISMISS_REASONS: readonly {
  resolution: Exclude<SuggestionResolution, "none" | "action_taken">;
  label: string;
}[] = [
  { resolution: "already_done", label: "Already done" },
  { resolution: "useful", label: "Useful, no action needed" },
  { resolution: "not_relevant", label: "Not relevant" },
  { resolution: "incorrect", label: "Incorrect" },
  { resolution: "not_now", label: "Not now" },
];

export function suggestionFromWire(wire: SuggestionWire): Suggestion {
  return {
    id: wire.id,
    kind: wire.kind,
    title: wire.title,
    bodyMd: wire.body_md,
    links: wire.links ?? [],
    actions: (wire.actions ?? []).filter(
      (action) => typeof action?.label === "string" && typeof action?.prompt === "string",
    ),
    chart: wire.chart ?? null,
    sourceSessionId: wire.source_session_id,
    state: wire.state,
    resolution: wire.resolution ?? "none",
    createdAt: wire.created_at,
  };
}

/** One page of the viewer's suggestions in `states`, newest first. */
export async function listSuggestions(
  after: string | null,
  states: readonly SuggestionState[] = OPEN_SUGGESTION_STATES,
): Promise<SuggestionPage> {
  const params = new URLSearchParams({ limit: String(SUGGESTION_PAGE_SIZE) });
  for (const state of states) params.append("state", state);
  if (after !== null) params.set("after", after);
  const res = await authenticatedFetch(`/v1/inbox/items?${params}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const page = (await res.json()) as {
    data: SuggestionWire[];
    next_cursor: string | null;
    unread_count: number;
    unread_count_capped?: boolean;
  };
  return {
    data: page.data.map(suggestionFromWire),
    nextCursor: page.next_cursor,
    unreadCount: page.unread_count,
    unreadCountCapped: page.unread_count_capped === true,
  };
}

/** A PATCH body: mark read or unread, or close the item with a resolution. */
export type SuggestionUpdate =
  | { state: "read" | "unread" }
  | { resolution: "action_taken"; resolutionAction: number }
  | { resolution: Exclude<SuggestionResolution, "action_taken"> };

/** Record the viewer's response to one suggestion. */
export async function updateSuggestion(id: string, update: SuggestionUpdate): Promise<Suggestion> {
  const body =
    "state" in update
      ? { state: update.state }
      : update.resolution === "action_taken"
        ? { resolution: update.resolution, resolution_action: update.resolutionAction }
        : { resolution: update.resolution };
  const res = await authenticatedFetch(`/v1/inbox/items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return suggestionFromWire((await res.json()) as SuggestionWire);
}

/** The state an update moves an item to, matching the server's rules. */
export function stateAfterUpdate(update: SuggestionUpdate): SuggestionState {
  if ("state" in update) return update.state;
  if (update.resolution === "none") return "read";
  return update.resolution === "action_taken" || update.resolution === "already_done"
    ? "done"
    : "dismissed";
}

/** Apply an update to cached pages, adjusting the unread count to match. */
export function applySuggestionUpdate(
  pages: SuggestionPage[],
  id: string,
  update: SuggestionUpdate,
): SuggestionPage[] {
  const nextState = stateAfterUpdate(update);
  let delta = 0;
  const updated = pages.map((page) => ({
    ...page,
    data: page.data.flatMap((item) => {
      if (item.id !== id) return [item];
      if (item.state === "unread" && nextState !== "unread") delta -= 1;
      if (item.state !== "unread" && nextState === "unread") delta += 1;
      return OPEN_SUGGESTION_STATES.includes(nextState) ? [{ ...item, state: nextState }] : [];
    }),
  }));
  // Every page carries the same total; a capped total stays as the server sent it.
  return updated.map((page) =>
    page.unreadCountCapped ? page : { ...page, unreadCount: Math.max(0, page.unreadCount + delta) },
  );
}

/** The badge text for an unread count, e.g. `"100+"` once the server stops counting. */
export function unreadCountText(count: number, capped: boolean): string {
  return capped ? `${count}+` : String(count);
}

/** Where a link goes: a session in this app, an external http(s) page, or nowhere. */
export type SuggestionLinkTarget =
  { kind: "session"; path: string } | { kind: "external"; href: string } | null;

/** An http(s) URL for an agent-written reference, or `null` for anything else. */
export function safeWebHref(ref: string): string | null {
  const trimmed = ref.trim();
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed.includes("://") ? trimmed : `https://${trimmed}`);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export function suggestionLinkTarget(link: SuggestionLink): SuggestionLinkTarget {
  const ref = link.ref.trim();
  if (!ref) return null;
  if (link.type === "session") return { kind: "session", path: `/c/${encodeURIComponent(ref)}` };
  // Agents write these refs, so only plain web links are rendered as links.
  const href = safeWebHref(ref);
  return href ? { kind: "external", href } : null;
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
