import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { agentRootName } from "@/lib/forkHarness";
import { capitalizeAgentName } from "@/lib/agentLabels";
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
  // Creation epoch of a catalog agent — recency signal for same-name
  // supersession. Deliberately NOT updated_at: `--agent` re-registration
  // rewrites a template's bundle on every server restart (non-reproducible
  // tar), bumping updated_at/version for unchanged content — which would let
  // a restarted template spuriously beat a newer upload. created_at is
  // immutable, so it is the stable signal. Omitted on older servers and on
  // session-derived agents (whose recency comes from the scanned session).
  created_at?: number | null;
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
    if (nativeAgent?.key !== "kiro") {
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
  const res = await authenticatedFetch("/v1/agents");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data: BuiltinAgentWire[] };
  return dedupeNativeAgents(
    body.data.map((a) => ({
      id: a.id,
      name: a.name,
      display_name: displayNameForAgent(a.name, a.harness),
      description: a.description ?? null,
      harness: a.harness ?? null,
      skills: a.skills ?? [],
      // Raw passthrough (not coerced): an absent value stays undefined so
      // older servers degrade to "protected" and existing strict-equality
      // tests (which ignore undefined props) are unaffected.
      builtin: a.builtin,
      created_at: a.created_at,
    })),
  );
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
 * degrades discovery scope rather than failing.
 */
async function scanSessionAgents(): Promise<ScannedSessionAgent[]> {
  // limit=100 bounds the scan to the most recent sessions: an agent whose
  // only session is older than the newest 100 won't be discovered. A
  // deliberate recency cut — the picker is for agents the user is
  // actively working with.
  const res = await authenticatedFetch("/v1/sessions?limit=100&kind=any");
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

/** Wire shape of `GET /v1/sessions/{id}/agent` (AgentObject). */
interface AgentObjectWire {
  id: string;
  name: string;
  description?: string | null;
  harness?: string | null;
  skills?: { name: string; description: string }[];
}

/**
 * Enrich one scanned session agent into the picker's AvailableAgent
 * shape via `GET /v1/sessions/{id}/agent` (description, harness,
 * bundled skills). On failure the agent is still listed with the
 * name-only fields from the scan — mirroring the server's own
 * `_to_agent_object` degradation: one unloadable bundle must not
 * break discovery.
 */
async function enrichSessionAgent(scanned: ScannedSessionAgent): Promise<AvailableAgent> {
  const fallback: AvailableAgent = {
    id: scanned.agentId,
    name: scanned.agentName,
    display_name: displayNameForAgent(scanned.agentName),
    description: null,
    harness: null,
    skills: [],
    // builtin/created_at intentionally omitted: session-derived agents never
    // seed the catalog, and their recency comes from the scanned session's
    // createdAt (used directly in the dedup), not from this object.
  };
  try {
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(scanned.sessionId)}/agent`,
    );
    if (!res.ok) return fallback;
    const json = (await res.json()) as AgentObjectWire;
    return {
      ...fallback,
      display_name: displayNameForAgent(json.name, json.harness),
      description: json.description ?? null,
      harness: json.harness ?? null,
      skills: json.skills ?? [],
    };
  } catch {
    // Network-level failure — same best-effort degradation as the
    // non-ok branch above: list the agent from scan fields.
    return fallback;
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
 */
async function fetchAvailableAgents(): Promise<AvailableAgent[]> {
  const [catalog, scanned] = await Promise.all([
    fetchBuiltinAgents(),
    scanSessionAgents().catch(() => [] as ScannedSessionAgent[]),
  ]);
  // Seeded built-ins are emitted verbatim and protected; user-registered
  // templates seed the newest-wins buckets so an upload can supersede them.
  // `builtin !== false` keeps both true (seeded) and undefined (older server,
  // no flag) protected — only an explicit false marks a supersedable
  // user-registered template.
  const seeded = catalog.filter((a) => a.builtin !== false);
  const userTemplates = catalog.filter((a) => a.builtin === false);
  const catalogIds = new Set(catalog.map((a) => a.id));
  const seededNames = new Set(seeded.map((a) => a.name));
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
  // defensively anyway.
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

  const resolved = (
    await Promise.all(
      Array.from(byName.values()).map((c) =>
        c.template !== null ? Promise.resolve(c.template) : enrichSessionAgent(c.scanned!),
      ),
    )
  ).filter((agent) => {
    const nativeKey = nativeCodingAgentForAvailableAgent(agent)?.key;
    return nativeKey !== "kiro" || !hasKiroBuiltin;
  });
  // Seeded built-ins first; user templates / custom uploads follow, newest
  // first. NewChatDialog's display-order sort is stable, so unranked names
  // keep this relative order.
  resolved.sort((a, b) => recencyOf(b) - recencyOf(a));
  return [...seeded, ...resolved];
}

interface UseAvailableAgentsOptions {
  enabled?: boolean;
}

export function useAvailableAgents(options: UseAvailableAgentsOptions = {}) {
  return useQuery({
    queryKey: ["available-agents"],
    queryFn: fetchAvailableAgents,
    enabled: options.enabled ?? true,
    staleTime: 30_000,
  });
}
