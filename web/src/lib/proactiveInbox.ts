// Agent-posted Inbox suggestions (`/v1/inbox/items`): wire types, the API
// calls, and pure helpers the Inbox page renders from. Kept free of React so
// the mapping, cache updates, and link checks are unit-testable.
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type SuggestionKind = "followup" | "info" | "reminder";
export type SuggestionStatus = "unread" | "read" | "done" | "dismissed";
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
/**
 * One rich-content block on a suggestion, such as `{type: "chart", chart: {...}}`.
 * Its content is unvalidated; the block's renderer checks it and skips what it
 * cannot draw.
 */
export interface SuggestionMediaBlock {
  type: string;
  [field: string]: unknown;
}

export interface Suggestion {
  id: string;
  kind: SuggestionKind;
  title: string;
  bodyMd: string;
  links: SuggestionLink[];
  actions: SuggestionAction[];
  /** Rich content drawn on the card, in order. */
  media: SuggestionMediaBlock[];
  sourceSessionId: string;
  status: SuggestionStatus;
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
  media?: unknown;
  source_session_id: string;
  status: SuggestionStatus;
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
export const OPEN_SUGGESTION_STATUSES: readonly SuggestionStatus[] = ["unread", "read"];

/** What the UI calls the proactive agent; the one place to change when it is renamed. */
export const PROACTIVE_AGENT_DISPLAY_NAME = "Omni Advisor";
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
    media: Array.isArray(wire.media)
      ? wire.media.filter(
          (block): block is SuggestionMediaBlock =>
            typeof block === "object" && block !== null && typeof block.type === "string",
        )
      : [],
    sourceSessionId: wire.source_session_id,
    status: wire.status,
    resolution: wire.resolution ?? "none",
    createdAt: wire.created_at,
  };
}

/** One page of the viewer's suggestions in `statuses`, newest first. */
export async function listSuggestions(
  after: string | null,
  statuses: readonly SuggestionStatus[] = OPEN_SUGGESTION_STATUSES,
): Promise<SuggestionPage> {
  const params = new URLSearchParams({ limit: String(SUGGESTION_PAGE_SIZE) });
  for (const status of statuses) params.append("status", status);
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
  | { status: "read" | "unread" }
  | { resolution: "action_taken"; resolutionAction: number }
  | { resolution: Exclude<SuggestionResolution, "action_taken"> };

/** Record the viewer's response to one suggestion. */
export async function updateSuggestion(id: string, update: SuggestionUpdate): Promise<Suggestion> {
  const body =
    "status" in update
      ? { status: update.status }
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

/** The status an update moves an item to, matching the server's rules. */
export function statusAfterUpdate(update: SuggestionUpdate): SuggestionStatus {
  if ("status" in update) return update.status;
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
  const nextStatus = statusAfterUpdate(update);
  let delta = 0;
  const updated = pages.map((page) => ({
    ...page,
    data: page.data.flatMap((item) => {
      if (item.id !== id) return [item];
      if (item.status === "unread" && nextStatus !== "unread") delta -= 1;
      if (item.status !== "unread" && nextStatus === "unread") delta += 1;
      return OPEN_SUGGESTION_STATUSES.includes(nextStatus) ? [{ ...item, status: nextStatus }] : [];
    }),
  }));
  // Every page carries the same total; a capped total stays as the server sent it.
  return updated.map((page) =>
    page.unreadCountCapped ? page : { ...page, unreadCount: Math.max(0, page.unreadCount + delta) },
  );
}

/**
 * Write the server's copy of one suggestion into cached pages after its update
 * succeeds: replace an open item, drop a resolved one, and move the unread count
 * by the difference from the cached copy. An item the cache no longer holds is
 * left for the next fetch to place.
 */
export function reconcileSuggestion(pages: SuggestionPage[], saved: Suggestion): SuggestionPage[] {
  const isOpen = OPEN_SUGGESTION_STATUSES.includes(saved.status);
  let delta = 0;
  const updated = pages.map((page) => ({
    ...page,
    data: page.data.flatMap((item) => {
      if (item.id !== saved.id) return [item];
      delta += Number(saved.status === "unread") - Number(item.status === "unread");
      return isOpen ? [saved] : [];
    }),
  }));
  return updated.map((page) =>
    page.unreadCountCapped ? page : { ...page, unreadCount: Math.max(0, page.unreadCount + delta) },
  );
}

/** Where a suggestion sits in cached pages, so a failed update can put it back. */
export type SuggestionPlace = { page: number; index: number; item: Suggestion };

export function locateSuggestion(pages: SuggestionPage[], id: string): SuggestionPlace | null {
  for (const [page, { data }] of pages.entries()) {
    const index = data.findIndex((item) => item.id === id);
    if (index !== -1) return { page, index, item: data[index] };
  }
  return null;
}

/**
 * Put a suggestion back the way it was before an update that then failed:
 * replace it where it still is, or reinsert it where it was, and move the unread
 * count back to match.
 */
export function restoreSuggestion(
  pages: SuggestionPage[],
  place: SuggestionPlace,
): SuggestionPage[] {
  if (locateSuggestion(pages, place.item.id)) return reconcileSuggestion(pages, place.item);
  const delta = Number(place.item.status === "unread");
  const restored = pages.map((page, number) => {
    if (number !== Math.min(place.page, pages.length - 1)) return page;
    const data = [...page.data];
    data.splice(Math.min(place.index, data.length), 0, place.item);
    return { ...page, data };
  });
  return restored.map((page) =>
    page.unreadCountCapped ? page : { ...page, unreadCount: page.unreadCount + delta },
  );
}

/** The badge text for an unread count, e.g. `"100+"` once the server stops counting. */
export function unreadCountText(count: number, capped: boolean): string {
  return capped ? `${count}+` : String(count);
}

/** Where a link goes: a session in this app, an external http(s) page, or nowhere. */
export type SuggestionLinkTarget =
  { kind: "session"; path: string } | { kind: "external"; href: string } | null;

/**
 * The URL for an agent-written reference when it is a plain web link: an
 * explicit http(s) scheme, a host, and no user name or password (the server
 * applies the same rule). `null` for anything else.
 */
export function safeWebHref(ref: string): string | null {
  const trimmed = ref.trim();
  if (!/^https?:\/\//i.test(trimmed)) return null;
  try {
    const url = new URL(trimmed);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (!url.hostname || url.username || url.password) return null;
    return url.toString();
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
