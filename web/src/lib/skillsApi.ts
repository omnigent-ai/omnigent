import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** A repository the skills marketplace browses (``GET /v1/skill-sources``). */
export interface SkillSource {
  id: string;
  url: string;
  ref: string | null;
  name: string;
  created_by: string | null;
  created_at: number;
  /** Listed in the server config: seeded at startup and not removable here. */
  managed: boolean;
}

/** One skill a repository offers, with the caller's installs of it if any. */
export interface CatalogSkill {
  name: string;
  display_name: string | null;
  description: string;
  path: string;
  plugin: string | null;
  /** The caller's own install, else the org's. */
  installed_id: string | null;
  /** Every scope it is installed in; absent on older servers. */
  installed_scopes?: SkillScope[];
}

export interface SkillCatalog {
  url: string;
  ref: string | null;
  commit: string | null;
  skills: CatalogSkill[];
  skipped: string[];
}

export type SkillScope = "user" | "org";

/** A skill installed server-side (``GET /v1/installed-skills``). */
export interface InstalledSkill {
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  source_url: string;
  /** ``owner/repo`` label of the source. */
  source: string;
  ref: string | null;
  commit: string | null;
  path: string;
  scope: SkillScope;
  owner_user_id: string | null;
  enabled: boolean;
  installed_by: string | null;
  installed_at: number;
  updated_at: number | null;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

/** One page of a keyset-paginated list; pass `last_id` as `after` for the next. */
export interface Page<T> {
  data: T[];
  has_more: boolean;
  last_id: string | null;
}

export async function listSkillSources(
  after: string | null = null,
  signal?: AbortSignal,
): Promise<Page<SkillSource>> {
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after", after);
  const res = await authenticatedFetch(`/v1/skill-sources?${params}`, { signal });
  return json<Page<SkillSource>>(res);
}

export async function addSkillSource(input: {
  source: string;
  ref?: string | null;
  name?: string | null;
}): Promise<SkillSource> {
  const res = await authenticatedFetch("/v1/skill-sources", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  return json<SkillSource>(res);
}

export async function deleteSkillSource(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/skill-sources/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

export async function fetchSourceCatalog(
  id: string,
  refresh = false,
  signal?: AbortSignal,
): Promise<SkillCatalog> {
  const params = refresh ? "?refresh=true" : "";
  const res = await authenticatedFetch(
    `/v1/skill-sources/${encodeURIComponent(id)}/catalog${params}`,
    { signal },
  );
  return json<SkillCatalog>(res);
}

/** Index an arbitrary GitHub repository (the Add from URL path). */
export async function previewCatalog(input: {
  source: string;
  ref?: string | null;
}): Promise<SkillCatalog> {
  const res = await authenticatedFetch("/v1/skill-catalog", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  return json<SkillCatalog>(res);
}

export async function listInstalledSkills(
  after: string | null = null,
  signal?: AbortSignal,
): Promise<Page<InstalledSkill>> {
  const params = new URLSearchParams({ limit: "50" });
  if (after) params.set("after", after);
  const res = await authenticatedFetch(`/v1/installed-skills?${params}`, { signal });
  return json<Page<InstalledSkill>>(res);
}

export async function installSkills(input: {
  source: string;
  ref?: string | null;
  names: string[];
  scope?: SkillScope;
}): Promise<InstalledSkill[]> {
  const res = await authenticatedFetch("/v1/installed-skills", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  return (await json<{ data: InstalledSkill[] }>(res)).data;
}

export async function updateInstalledSkill(id: string): Promise<InstalledSkill> {
  const res = await authenticatedFetch(`/v1/installed-skills/${encodeURIComponent(id)}/update`, {
    method: "POST",
  });
  return json<InstalledSkill>(res);
}

export async function setInstalledSkillEnabled(
  id: string,
  enabled: boolean,
): Promise<InstalledSkill> {
  const res = await authenticatedFetch(`/v1/installed-skills/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify({ enabled }),
  });
  return json<InstalledSkill>(res);
}

export async function deleteInstalledSkill(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/installed-skills/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

export async function fetchInstalledSkillContent(
  id: string,
  signal?: AbortSignal,
): Promise<string> {
  const res = await authenticatedFetch(`/v1/installed-skills/${encodeURIComponent(id)}/content`, {
    signal,
  });
  return (await json<{ markdown: string }>(res)).markdown;
}
