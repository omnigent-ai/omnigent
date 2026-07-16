/**
 * Typed client for the per-user sidebar preference endpoints
 * (`GET /v1/preferences`, `PUT /v1/preferences/{key}`).
 *
 * Backs the sidebar's pin set and its section collapse/expand sets so they
 * follow the account rather than living in one browser's `localStorage`. The
 * server copy is the source of truth; `localStorage` stays as a fast local
 * cache and offline fallback (see `useSidebarPreferences`).
 *
 * Mirrors `omnigent/server/routes/preferences.py`.
 */

import { authenticatedFetch } from "./identity";

/** The sidebar preference keys the server accepts (its allow-list). */
export type SidebarPreferenceKey =
  | "pinned_conversation_ids"
  | "collapsed_sidebar_sections"
  | "expanded_project_sections";

const SIDEBAR_PREFERENCE_KEYS: readonly SidebarPreferenceKey[] = [
  "pinned_conversation_ids",
  "collapsed_sidebar_sections",
  "expanded_project_sections",
];

/**
 * A user's stored sidebar preferences. Each key is absent when the user has
 * never written it (so the caller keeps whatever the local cache holds).
 */
export type SidebarPreferences = Partial<Record<SidebarPreferenceKey, string[]>>;

/**
 * Fetch every stored sidebar preference for the current user.
 *
 * Rejects when the endpoint is unavailable (unauthenticated, single-user
 * server without the route, or offline) so the caller falls back to
 * `localStorage`. Only well-formed string-array values survive; anything else
 * is dropped, leaving that key to the local cache.
 */
export async function getSidebarPreferences(): Promise<SidebarPreferences> {
  const res = await authenticatedFetch("/v1/preferences");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { preferences?: Record<string, unknown> };
  const raw = body.preferences ?? {};
  const out: SidebarPreferences = {};
  for (const key of SIDEBAR_PREFERENCE_KEYS) {
    const value = raw[key];
    if (Array.isArray(value) && value.every((v): v is string => typeof v === "string")) {
      out[key] = value;
    }
  }
  return out;
}

/** Upsert one sidebar preference for the current user. */
export async function putSidebarPreference(
  key: SidebarPreferenceKey,
  value: string[],
): Promise<void> {
  const res = await authenticatedFetch(`/v1/preferences/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  if (!res.ok) {
    const parsed = (await res.json().catch(() => ({}))) as {
      error?: { message?: string };
    };
    throw new Error(parsed?.error?.message ?? `${res.status} ${res.statusText}`);
  }
}
