import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useSidebarData } from "./useSidebarData";
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
  // Shown while discovery loads; the skills endpoint returns the effective catalog.
  // Empty on older servers without the field.
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
  // immutable, so it is the stable signal. Omitted on older servers.
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

/**
 * Fetch the built-in agents from the read-only list `GET /v1/agents`
 * (see designs/BUILTIN_AGENTS.md).
 */
export async function fetchAgentCatalog(): Promise<AvailableAgent[]> {
  const rows: BuiltinAgentWire[] = [];
  let after: string | null = null;
  // Each page provides the cursor for the next request.
  /* oxlint-disable no-await-in-loop */
  do {
    const params = new URLSearchParams();
    if (after !== null) params.set("after", after);
    const url = params.size ? `/v1/agents?${params}` : "/v1/agents";
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

interface DiscoveredSessionAgent {
  agentId: string;
  agentName: string;
  createdAt: number | null;
  agent: AvailableAgent;
}

/** Discover agents from the first 30 owned sessions already loaded by the sidebar. */
export function useSessionAgents(enabled = true) {
  const { mine } = useSidebarData();
  const data = useMemo(() => {
    if (!enabled || !mine.data) return undefined;
    const agents = new Map<string, AvailableAgent>();
    for (const row of mine.data.pages.flatMap((page) => page.data).slice(0, 30)) {
      if (!row.agent_id || !row.agent_name || agents.has(row.agent_id)) continue;
      agents.set(row.agent_id, {
        id: row.agent_id,
        name: row.agent_name,
        display_name: displayNameForAgent(row.agent_name),
        description: null,
        harness: null,
        skills: [],
        sessionId: row.id,
        created_at: row.created_at,
      });
    }
    return [...agents.values()];
  }, [enabled, mine.data]);
  return { ...mine, data };
}

/** Wire shape of `GET /v1/sessions/{id}/agent` (AgentObject). */
interface AgentObjectWire {
  id: string;
  name: string;
  description?: string | null;
  harness?: string | null;
  skills?: { name: string; description: string }[];
}

function sessionAgentFromDiscovery(discovered: DiscoveredSessionAgent): AvailableAgent {
  return discovered.agent;
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

/** Keep configured agent ids through name deduplication within the available sources. */
async function fetchAvailableAgents(
  pinnedAgentIds: string[],
  queryClient: QueryClient,
  sessionAgents: AvailableAgent[],
): Promise<AvailableAgent[]> {
  const catalog = await queryClient.fetchQuery({
    queryKey: AGENT_CATALOG_QUERY_KEY,
    queryFn: fetchAgentCatalog,
    staleTime: AVAILABLE_AGENTS_STALE_MS,
  });
  const discovered = sessionAgents.map((agent) => ({
    agentId: agent.id,
    agentName: agent.name,
    createdAt: agent.created_at ?? null,
    agent,
  }));
  const merged = mergeAvailableAgents(catalog, discovered);
  for (const id of pinnedAgentIds) {
    if (merged.some((agent) => agent.id === id)) continue;
    const agent = sessionAgents.find((a) => a.id === id) ?? catalog.find((a) => a.id === id);
    if (agent) merged.push(agent);
  }
  return merged;
}

function mergeAvailableAgents(
  catalog: AvailableAgent[],
  discovered: DiscoveredSessionAgent[],
): AvailableAgent[] {
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

  // Choose the newest catalog candidate for each base name.
  interface Candidate {
    recency: number;
    template: AvailableAgent | null;
    discovered: DiscoveredSessionAgent | null;
  }
  const byName = new Map<string, Candidate>();

  // Seed with user-registered templates. A template name is globally unique
  // among catalog rows, so it cannot collide with a seeded built-in; guard
  // defensively anyway. Rooting seeded names also drops stale fork rows from
  // older catalogs once their canonical built-in is present.
  for (const t of userTemplates) {
    const base = agentRootName(t.name);
    if (seededNames.has(base)) continue;
    byName.set(base, { recency: recencyOf(t), template: t, discovered: null });
  }

  for (const agent of discovered) {
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
    // so equal recency keeps the FIRST seen — the discovery is newest-first, so
    // ties preserve the catalog ordering.
    const recency = agent.createdAt ?? 0;
    const existing = byName.get(base);
    if (!existing || recency > existing.recency) {
      byName.set(base, { recency, template: null, discovered: agent });
    }
  }

  const resolved = Array.from(byName.values())
    .map((c) => (c.template !== null ? c.template : sessionAgentFromDiscovery(c.discovered!)))
    .filter((agent) => {
      const nativeKey = nativeCodingAgentForAvailableAgent(agent)?.key;
      return nativeKey !== "kiro" || !hasKiroBuiltin;
    });
  // Seeded built-ins first; user templates / custom uploads follow, newest
  // first. NewChatDialog's display-order sort is stable, so unranked names
  // keep this relative order.
  resolved.sort((a, b) => recencyOf(b) - recencyOf(a));
  return [...seeded, ...resolved];
}

// Catalog-only list backing the placeholder rows. Shared by the hook's
// catalog query and the merged queryFn (via fetchQuery), so a picker mount
// issues a single GET /v1/agents for both.
const AGENT_CATALOG_QUERY_KEY = ["available-agents-catalog"] as const;
const AVAILABLE_AGENTS_STALE_MS = 30_000;

interface UseAvailableAgentsOptions {
  enabled?: boolean;
  /**
   * Preserve configured agent ids even when another agent wins the same-name merge.
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
  const sessionAgents = useSessionAgents(enabled);
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
  const queryClient = useQueryClient();
  // The harness/built-in rows come entirely from GET /v1/agents, so they must
  // not wait for a slow Mine session load (managed deployments with
  // large session tables). This catalog query resolves fast and feeds the
  // merged query's placeholder below; same enabled gate as the merged query.
  const catalogQuery = useQuery({
    queryKey: AGENT_CATALOG_QUERY_KEY,
    queryFn: () => fetchAgentCatalog(),
    enabled,
    staleTime: AVAILABLE_AGENTS_STALE_MS,
  });
  const { data: catalog } = catalogQuery;
  // Catalog merged with an empty discovery — the same rows a failing discovery degrades
  // to — shown while the merged fetch is in flight and upgraded in place when
  // the discovery lands. Consumers that must not resolve stored ids against a
  // partial list read isPlaceholderData to tell this state apart.
  //
  const placeholderData = useMemo(
    () => (catalog === undefined ? undefined : mergeAvailableAgents(catalog, [])),
    [catalog],
  );
  const query = useQuery({
    // Recompute when the first 30 Mine sessions change; hover patches match the prefix.
    queryKey: ["available-agents", pinnedKey, sessionAgents.data ?? null],
    // fetchQuery dedupes with the catalog query's in-flight fetch, so the
    // merged fetch reuses (not repeats) the catalog request.
    queryFn: () =>
      fetchAvailableAgents(
        pinnedKey === "" ? [] : pinnedKey.split(","),
        queryClient,
        sessionAgents.data ?? [],
      ),
    enabled: enabled && (sessionAgents.data !== undefined || sessionAgents.isError),
    staleTime: AVAILABLE_AGENTS_STALE_MS,
    placeholderData,
    select,
  });
  return {
    ...query,
    isLoading: query.isLoading || (enabled && query.isPending && catalogQuery.isPending),
  };
}
