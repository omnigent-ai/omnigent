import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { agentRootName } from "@/lib/forkHarness";
import { capitalizeAgentName, useAcpHarnessIds, useHarnessLabels } from "@/lib/agentLabels";
import {
  nativeCodingAgentForAvailableAgent,
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
} from "@/lib/nativeCodingAgents";

export interface AvailableAgent {
  id: string;
  name: string;
  display_name: string;
  description: string | null;
  // Harness/kind from GET /v1/agents, e.g. "codex", "codex-native",
  // "claude-native", or "claude-sdk". null when the server couldn't load
  // the agent's spec. Lets the picker recognise Codex vs Claude agents
  // by kind rather than by name slug.
  harness: string | null;
  // Skills bundled in the agent spec (name + one-line description).
  // Feeds the landing composer's "/" menu before a session exists;
  // host-discovered skills only resolve once a runner is bound, so
  // they're absent here. Empty on older servers without the field.
  skills: { name: string; description: string }[];
  // Server-seeded built-in (deterministic, name-derived id) vs a
  // user-registered template. Only set on catalog rows from GET /v1/agents;
  // omitted on session-derived agents and on older servers without the field
  // (where a missing value is treated as protected, preserving prior
  // shadow-everything behavior). The picker protects seeded built-ins from a
  // same-named `omnigent run` upload, but lets a newer upload supersede a
  // user-registered template (builtin === false).
  builtin?: boolean;
  // True when the server declares this agent's harness generic-ACP (harness
  // catalog ``integration_mode === "acp-subprocess"``) — a builtin ACP CLI row
  // (devin / grok) or a user-configured ``acp:<slug>`` agent. Stamped on by
  // {@link useAvailableAgents}; absent until the catalog loads and on servers
  // that don't report capabilities, where grouping falls back to the id
  // heuristic in ``agentGrouping``.
  acpHarness?: boolean;
  // Creation epoch of a catalog agent — recency signal for same-name
  // supersession. Deliberately NOT updated_at: `--agent` re-registration
  // rewrites a template's bundle on every server restart (non-reproducible
  // tar), bumping updated_at/version for unchanged content — which would let
  // a restarted template spuriously beat a newer upload. created_at is
  // immutable, so it is the stable signal. Omitted on older servers and on
  // session-derived agents (whose recency comes from the scanned session).
  created_at?: number | null;
  // Session id used to fetch the full agent spec on hover. Only set on
  // session-discovered agents (custom uploads); absent on catalog agents
  // whose full data is already present from GET /v1/agents.
  sessionId?: string;
}

const DISPLAY_NAMES: Record<string, string> = {
  // nessie is no longer seeded, but older deployments retain their row.
  nessie: "Nessie",
  polly: "Polly",
  debby: "Debby",
};

function displayNameForAgent(name: string, harness?: string | null): string {
  return (
    nativeCodingAgentForHarness(harness)?.displayName ??
    nativeCodingAgentForAgentName(name)?.displayName ??
    DISPLAY_NAMES[name] ??
    capitalizeAgentName(name)
  );
}

function dedupeNativeAgents(agents: AvailableAgent[]): AvailableAgent[] {
  const result: AvailableAgent[] = [];
  const nativeIndex = new Map<string, number>();
  for (const agent of agents) {
    const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
    if (nativeAgent === undefined) {
      result.push(agent);
      continue;
    }
    const existingIndex = nativeIndex.get(nativeAgent.key);
    if (existingIndex === undefined) {
      nativeIndex.set(nativeAgent.key, result.length);
      result.push(agent);
      continue;
    }
    const existing = result[existingIndex];
    if (agent.name === nativeAgent.agentName && existing.name !== nativeAgent.agentName) {
      result[existingIndex] = agent;
    }
  }
  return result;
}

/** Wire row of the built-in list, GET /v1/agents. */
interface BuiltinAgentWire {
  id: string;
  name: string;
  description?: string | null;
  harness?: string | null;
  skills?: { name: string; description: string }[];
  // True only for server-seeded built-ins (deterministic id). Absent on
  // older servers, where every catalog row degrades to a protected entry.
  builtin?: boolean;
  created_at?: number | null;
}

interface BuiltinAgentsListWire {
  data: BuiltinAgentWire[];
  has_more?: boolean;
  last_id?: string | null;
}

/** Wire row of the sessions scan, GET /v1/sessions?kind=any. */
interface SessionListItemWire {
  id: string;
  agent_id?: string | null;
  agent_name?: string | null;
  // Session creation epoch — proxy for "when the user last ran this agent",
  // used to pick the newest among same-named uploads / templates.
  created_at?: number | null;
}

/**
 * Fetch the built-in agents from the read-only list `GET /v1/agents`
 * (see designs/BUILTIN_AGENTS.md).
 */
async function fetchBuiltinAgents(): Promise<AvailableAgent[]> {
  const rows: BuiltinAgentWire[] = [];
  let after: string | null = null;
  // Each page provides the cursor for the next request.
  /* oxlint-disable no-await-in-loop */
  do {
    const url = after == null ? "/v1/agents" : `/v1/agents?after=${encodeURIComponent(after)}`;
    const res = await authenticatedFetch(url);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const body = (await res.json()) as BuiltinAgentsListWire;
    rows.push(...body.data);
    after = body.has_more === true && body.last_id ? body.last_id : null;
  } while (after != null);
  /* oxlint-enable no-await-in-loop */

  return rows.map((a) => ({
    id: a.id,
    name: a.name,
    display_name: displayNameForAgent(a.name, a.harness),
    description: a.description ?? null,
    harness: a.harness ?? null,
    skills: a.skills ?? [],
    // Omit rather than set to undefined so toEqual comparisons aren't
    // sensitive to absent-vs-undefined. Logic that reads builtin treats
    // undefined as "protected" (same as true), so omission is safe.
    ...(a.builtin !== undefined ? { builtin: a.builtin } : {}),
    ...(a.created_at !== undefined ? { created_at: a.created_at } : {}),
  }));
}

/**
 * A unique session-bound agent discovered by the sessions scan, paired
 * with one session it was seen on (used to fetch the full AgentObject
 * via `GET /v1/sessions/{id}/agent`, which is keyed by session id).
 */
interface ScannedSessionAgent {
  agentId: string;
  agentName: string;
  sessionId: string;
  // Creation epoch of the session it was seen on — recency proxy for
  // newest-wins supersession. null when the server omits created_at.
  createdAt: number | null;
}

/**
 * Scan the caller's sessions — sub-agent children included — for unique
 * bound agents. `kind=any` requires server support; an older server
 * ignores the unknown param and returns only top-level sessions, which
 * degrades discovery scope rather than failing. Archived sessions are
 * included: archiving a session must not make its (possibly still
 * deployed) agent undiscoverable — e.g. a project-pinned agent whose
 * anchor session was archived.
 */
async function scanSessionAgents(): Promise<ScannedSessionAgent[]> {
  // limit=100 bounds the scan to the most recent sessions: an agent whose
  // only session is older than the newest 100 won't be discovered. A
  // deliberate recency cut — the picker is for agents the user is
  // actively working with.
  const res = await authenticatedFetch("/v1/sessions?limit=100&kind=any&include_archived=true");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data: SessionListItemWire[] };
  const seen = new Map<string, ScannedSessionAgent>();
  for (const session of body.data) {
    // Rows without an agent_name are orphaned (agent row deleted); skip
    // them, matching useAgents' sessions-derived list.
    if (!session.agent_id || !session.agent_name) continue;
    if (seen.has(session.agent_id)) continue;
    seen.set(session.agent_id, {
      agentId: session.agent_id,
      agentName: session.agent_name,
      sessionId: session.id,
      createdAt: session.created_at ?? null,
    });
  }
  return Array.from(seen.values());
}

/**
 * Direct lookup for a pinned agent the bounded scan missed (its only
 * sessions are archived or paginated out of the newest 100): any one
 * session bound to it names it and anchors the on-hover detail fetch.
 * null when no such session is visible to the caller — the agent is
 * genuinely unresolvable and the consumer surfaces that explicitly.
 */
async function lookupPinnedAgent(agentId: string): Promise<AvailableAgent | null> {
  try {
    const res = await authenticatedFetch(
      `/v1/sessions?limit=1&kind=any&include_archived=true&agent_id=${encodeURIComponent(agentId)}`,
    );
    if (!res.ok) return null;
    const body = (await res.json()) as { data: SessionListItemWire[] };
    const row = body.data[0];
    if (!row?.agent_id || !row.agent_name) return null;
    return sessionAgentFromScan({
      agentId: row.agent_id,
      agentName: row.agent_name,
      sessionId: row.id,
      createdAt: row.created_at ?? null,
    });
  } catch {
    return null;
  }
}

/** Wire shape of `GET /v1/sessions/{id}/agent` (AgentObject). */
interface AgentObjectWire {
  id: string;
  name: string;
  description?: string | null;
  harness?: string | null;
  skills?: { name: string; description: string }[];
}

/**
 * Build an AvailableAgent from session scan data alone — no extra fetch.
 * description, harness, and skills are null/empty and filled in on hover
 * via prefetchAvailableAgentDetails.
 */
function sessionAgentFromScan(scanned: ScannedSessionAgent): AvailableAgent {
  return {
    id: scanned.agentId,
    name: scanned.agentName,
    display_name: displayNameForAgent(scanned.agentName),
    description: null,
    harness: null,
    skills: [],
    sessionId: scanned.sessionId,
    // builtin/created_at intentionally omitted: session-derived agents never
    // seed the catalog, and their recency comes from the scanned session's
    // createdAt (used directly in the dedup), not from this object.
  };
}

/**
 * Fetch harness, description, and skills for a session-discovered agent and
 * patch them into the ["available-agents"] cache. Call on hover so the data
 * is ready before the user clicks — zero cost for agents they never hover.
 */
export async function prefetchAvailableAgentDetails(
  agent: AvailableAgent,
  queryClient: QueryClient,
): Promise<void> {
  if (!agent.sessionId || agent.harness !== null || agent.description !== null) return;
  try {
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(agent.sessionId)}/agent`,
    );
    if (!res.ok) return;
    const json = (await res.json()) as AgentObjectWire;
    // Prefix match: patches the bare list and every pinned variant alike.
    queryClient.setQueriesData<AvailableAgent[]>({ queryKey: ["available-agents"] }, (prev) => {
      if (!prev) return prev;
      const enriched = prev.map((a) =>
        a.id !== agent.id
          ? a
          : {
              ...a,
              display_name: displayNameForAgent(json.name, json.harness),
              description: json.description ?? null,
              harness: json.harness ?? null,
              skills: json.skills ?? [],
            },
      );
      // If enrichment reveals this agent is a native coding agent (e.g. a
      // kiro-native session with a non-canonical name), remove it when a
      // seeded built-in with the same native key already exists so it doesn't
      // surface as a duplicate picker row.
      const enrichedAgent = enriched.find((a) => a.id === agent.id);
      const enrichedKey = enrichedAgent
        ? nativeCodingAgentForAvailableAgent(enrichedAgent)?.key
        : undefined;
      if (enrichedKey) {
        const builtinExists = enriched.some(
          (a) => a.id !== agent.id && nativeCodingAgentForAvailableAgent(a)?.key === enrichedKey,
        );
        if (builtinExists) return enriched.filter((a) => a.id !== agent.id);
      }
      return enriched;
    });
  } catch {
    // Best-effort — agent stays name-only on failure.
  }
}

/**
 * The new-session picker's agent catalog: the catalog from
 * `GET /v1/agents` (seeded built-ins + user-registered templates), plus
 * custom agents discovered on the caller's sessions (sub-agent sessions
 * included) via `GET /v1/sessions?kind=any`.
 *
 * Two kinds of catalog row are handled differently when a same-named
 * `omnigent run` upload exists:
 *
 * - SEEDED built-ins (`builtin: true`, deterministic id) are protected:
 *   they always list verbatim, and a same-named upload (or a fork/switch
 *   clone of one — `agentRootName` peels every `"(fork <id>)"` layer) is
 *   dropped. The seeded agent is the canonical identity for its name.
 * - USER-registered templates (`builtin: false`, e.g. `--agent`) compete
 *   with same-named uploads on recency: the newest of {template, uploads}
 *   wins, so a fresh `omnigent run agent.yaml` supersedes a stale template
 *   instead of being shadowed by it. This is the fix for the picker binding
 *   an older version when a newer one was just run.
 *
 * Session rows binding a catalog agent directly (by id) are dropped — that
 * agent is already represented. Genuinely custom uploads (a local YAML mints
 * a fresh agent_id per session) collapse by base name, newest session
 * winning (#3234). Binding any survivor needs no new server support:
 * `POST /v1/sessions {agent_id}` already authorizes session-scoped agents
 * the caller can read.
 *
 * Older servers omit `builtin`, so every catalog row degrades to "protected"
 * — i.e. the prior shadow-everything behavior — rather than misclassifying.
 *
 * A failing sessions scan (e.g. transient 5xx) degrades to the catalog list
 * rather than blanking the picker — catalog availability must not be hostage
 * to the discovery extension.
 *
 * `pinnedAgentIds` (e.g. a project's configured default agent) are guaranteed
 * to survive: any pinned id the merged list lacks — dropped by the
 * newest-wins name collapse (an id swap would silently rebind the project),
 * or missed by the recency-bounded scan — is restored from the scan, from
 * the catalog (a session-less user template can lose its bucket to a newer
 * same-named upload), or resolved via a direct per-agent session lookup.
 * A pinned id in neither source stays absent (the consumer surfaces that
 * state).
 */
async function fetchAvailableAgents(pinnedAgentIds: string[] = []): Promise<AvailableAgent[]> {
  const [catalog, scanned] = await Promise.all([
    fetchBuiltinAgents(),
    scanSessionAgents().catch(() => [] as ScannedSessionAgent[]),
  ]);
  // Seeded built-ins are emitted verbatim and protected; user-registered
  // templates seed the newest-wins buckets so an upload can supersede them.
  // `builtin !== false` keeps both true (seeded) and undefined (older server,
  // no flag) protected — only an explicit false marks a supersedable
  // user-registered template.
  const seeded = dedupeNativeAgents(catalog.filter((a) => a.builtin !== false));
  const userTemplates = catalog.filter((a) => a.builtin === false);
  const catalogIds = new Set(catalog.map((a) => a.id));
  const seededNames = new Set(seeded.map((a) => agentRootName(a.name)));
  const hasKiroBuiltin = seeded.some((a) => nativeCodingAgentForAvailableAgent(a)?.key === "kiro");
  const kiroLegacyNames = new Set(["kiro"]);

  const recencyOf = (a: AvailableAgent): number => a.created_at ?? 0;

  // base name -> winning candidate, decided by recency. A resolved template
  // carries full info; a session candidate is enriched lazily below.
  interface Candidate {
    recency: number;
    template: AvailableAgent | null;
    scanned: ScannedSessionAgent | null;
  }
  const byName = new Map<string, Candidate>();

  // Seed with user-registered templates. A template name is globally unique
  // among catalog rows, so it cannot collide with a seeded built-in; guard
  // defensively anyway. Rooting seeded names also drops stale fork rows from
  // older catalogs once their canonical built-in is present.
  for (const t of userTemplates) {
    const base = agentRootName(t.name);
    if (seededNames.has(base)) continue;
    byName.set(base, { recency: recencyOf(t), template: t, scanned: null });
  }

  for (const agent of scanned) {
    // Peel EVERY clone layer: a fork of a fork is named
    // `"<name> (fork ag_a) (fork ag_b)"`, and a single-layer strip would
    // leave a non-matching name that slips the seeded-shadow check.
    const base = agentRootName(agent.agentName);
    // Bound a catalog agent directly (seeded built-in OR user template):
    // already represented (verbatim, or as a candidate above).
    if (catalogIds.has(agent.agentId)) continue;
    // Seeded built-in name (incl. fork/switch clones): the built-in wins.
    if (seededNames.has(base)) continue;
    if (hasKiroBuiltin && kiroLegacyNames.has(base.toLocaleLowerCase())) continue;
    // Genuine custom upload (or a clone of one). Newest same-named row wins,
    // superseding an older user-registered template seeded above. Strict `>`
    // so equal recency keeps the FIRST seen — the scan is newest-first, so
    // ties resolve to the newest session (matches prior collapse behavior).
    const recency = agent.createdAt ?? 0;
    const existing = byName.get(base);
    if (!existing || recency > existing.recency) {
      byName.set(base, { recency, template: null, scanned: agent });
    }
  }

  const resolved = Array.from(byName.values())
    .map((c) => (c.template !== null ? c.template : sessionAgentFromScan(c.scanned!)))
    .filter((agent) => {
      const nativeKey = nativeCodingAgentForAvailableAgent(agent)?.key;
      return nativeKey !== "kiro" || !hasKiroBuiltin;
    });
  // Seeded built-ins first; user templates / custom uploads follow, newest
  // first. NewChatDialog's display-order sort is stable, so unranked names
  // keep this relative order.
  resolved.sort((a, b) => recencyOf(b) - recencyOf(a));
  const merged = [...seeded, ...resolved];

  // Pinned-survival pass: restore every pinned id the merge lost (from the
  // scan when it saw the agent but a same-named newer row won its bucket),
  // from the catalog when the pin is a user-registered template with no
  // sessions of its own, or via a direct lookup when neither source saw it.
  const missingPinned = pinnedAgentIds.filter((id) => !merged.some((a) => a.id === id));
  const restored = await Promise.all(
    missingPinned.map((id) => {
      const seen = scanned.find((s) => s.agentId === id);
      if (seen) return Promise.resolve(sessionAgentFromScan(seen));
      const template = catalog.find((a) => a.id === id);
      return template ? Promise.resolve(template) : lookupPinnedAgent(id);
    }),
  );
  merged.push(...restored.filter((a): a is AvailableAgent => a !== null));
  return merged;
}

interface UseAvailableAgentsOptions {
  enabled?: boolean;
  /**
   * Agent ids that must survive discovery — e.g. the current project's
   * configured default agent, which the recency-bounded scan can miss and
   * the same-name collapse could otherwise drop or id-swap. Resolved via a
   * direct lookup when needed; ids with no visible session stay absent.
   */
  pinnedAgentIds?: string[];
}

/**
 * Stamp the server's generic-ACP identity onto fetched agents.
 *
 * The fetchers above can't read the harness catalog (it's a hook), so the ACP
 * flag and the vendor label are applied here instead. The label matters: a
 * seeded ACP agent's ``name`` is a slug, so the capitalization fallback renders
 * Grok Build as "Grok" and a user's "My Devin Agent" as "My-devin-agent". The
 * catalog carries the real label for both — the vendor's for a builtin row, the
 * user's own for a configured ``acp:<slug>`` agent.
 *
 * Returns the input array untouched when the catalog hasn't loaded, so a
 * consumer's ``useMemo`` doesn't churn on an equivalent copy.
 */
function applyAcpHarnessCatalog(
  agents: AvailableAgent[],
  acpHarnessIds: ReadonlySet<string>,
  harnessLabels: Record<string, string>,
): AvailableAgent[] {
  if (acpHarnessIds.size === 0) return agents;
  return agents.map((agent) => {
    const harness = agent.harness;
    if (harness == null || !acpHarnessIds.has(harness)) return agent;
    return {
      ...agent,
      acpHarness: true,
      display_name: harnessLabels[harness] ?? agent.display_name,
    };
  });
}

export function useAvailableAgents(options: UseAvailableAgentsOptions = {}) {
  const enabled = options.enabled ?? true;
  // Normalized, order-stable pin key so equivalent pin sets share one cache
  // entry and a caller's fresh array literal doesn't churn the query. Agent
  // ids never contain "," so the join is unambiguous.
  const pinnedIds = options.pinnedAgentIds;
  const pinnedKey = useMemo(
    () =>
      Array.from(new Set(pinnedIds ?? []))
        .sort()
        .join(","),
    [pinnedIds],
  );
  // Read the catalog under the same gate: a disabled picker must not provoke a
  // request. Both values are cached references, so the memoized select keeps a
  // stable identity and TanStack doesn't hand consumers a fresh array per render.
  const acpHarnessIds = useAcpHarnessIds(enabled);
  const harnessLabels = useHarnessLabels(enabled);
  const select = useMemo(
    () => (agents: AvailableAgent[]) =>
      applyAcpHarnessCatalog(agents, acpHarnessIds, harnessLabels),
    [acpHarnessIds, harnessLabels],
  );
  return useQuery({
    // Unpinned consumers keep the historical bare key; pinned variants get
    // their own entry (prefetch patches both via a prefix match).
    queryKey: pinnedKey === "" ? ["available-agents"] : ["available-agents", pinnedKey],
    queryFn: () => fetchAvailableAgents(pinnedKey === "" ? [] : pinnedKey.split(",")),
    enabled,
    staleTime: 30_000,
    select,
  });
}
