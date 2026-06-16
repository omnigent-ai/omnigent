import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { agentBaseName } from "@/lib/forkHarness";
import { capitalizeAgentName } from "@/lib/agentLabels";

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
}

const DISPLAY_NAMES: Record<string, string> = {
  "claude-native-ui": "Claude Code",
  "codex-native-ui": "Codex",
  "cursor-native-ui": "Cursor",
  "mimo-native-ui": "Mimo",
  "gemini-native-ui": "Gemini",
  "cmd-native-ui": "Command Code",
  // nessie is no longer seeded, but older deployments retain their row.
  nessie: "Nessie",
  polly: "Polly",
  "polly-codex": "Polly Codex",
  debby: "Debby",
};

/** Wire row of the built-in list, GET /v1/agents. */
interface BuiltinAgentWire {
  id: string;
  name: string;
  description?: string | null;
  harness?: string | null;
  skills?: { name: string; description: string }[];
}

/** Wire row of the sessions scan, GET /v1/sessions?kind=any. */
interface SessionListItemWire {
  id: string;
  agent_id?: string | null;
  agent_name?: string | null;
}

/**
 * Fetch the built-in agents from the read-only list `GET /v1/agents`
 * (see designs/BUILTIN_AGENTS.md).
 */
async function fetchBuiltinAgents(): Promise<AvailableAgent[]> {
  const res = await authenticatedFetch("/v1/agents");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data: BuiltinAgentWire[] };
  return body.data.map((a) => ({
    id: a.id,
    name: a.name,
    display_name: DISPLAY_NAMES[a.name] ?? capitalizeAgentName(a.name),
    description: a.description ?? null,
    harness: a.harness ?? null,
    skills: a.skills ?? [],
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
    display_name: DISPLAY_NAMES[scanned.agentName] ?? capitalizeAgentName(scanned.agentName),
    description: null,
    harness: null,
    skills: [],
  };
  try {
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(scanned.sessionId)}/agent`,
    );
    if (!res.ok) return fallback;
    const json = (await res.json()) as AgentObjectWire;
    return {
      ...fallback,
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
 * The new-session picker's agent catalog: built-in agents from
 * `GET /v1/agents`, plus custom agents discovered on the caller's
 * sessions (sub-agent sessions included) via
 * `GET /v1/sessions?kind=any`.
 *
 * Session-discovered agents that shadow a built-in are dropped: by id
 * (most sessions bind a built-in's agent row directly) and by clone
 * base name (fork/switch create per-session rows named
 * `"<builtin> (fork <id>)"`). What survives is genuinely custom —
 * ad-hoc uploaded agents that were previously invisible to the picker.
 * Surviving custom agents are then collapsed by base name, keeping the
 * newest session's row: a custom agent launched repeatedly from a local
 * YAML mints a fresh agent_id per session, so by-id dedup alone would
 * list one picker row per session (#3234).
 * Binding them needs no new server support: `POST /v1/sessions
 * {agent_id}` already authorizes session-scoped agents the caller can
 * read.
 *
 * A failing sessions scan (e.g. transient 5xx) degrades to the
 * built-in list rather than blanking the picker — built-in
 * availability must not be hostage to the discovery extension.
 */
async function fetchAvailableAgents(): Promise<AvailableAgent[]> {
  const [builtins, scanned] = await Promise.all([
    fetchBuiltinAgents(),
    scanSessionAgents().catch(() => [] as ScannedSessionAgent[]),
  ]);
  const builtinIds = new Set(builtins.map((a) => a.id));
  const builtinNames = new Set(builtins.map((a) => a.name));
  // One row per custom base name, newest session first (scan order):
  // same-named agent_ids are per-session mints of the same agent, and
  // identical-name rows are indistinguishable in the picker anyway.
  const customByName = new Map<string, ScannedSessionAgent>();
  for (const agent of scanned) {
    const base = agentBaseName(agent.agentName);
    if (builtinIds.has(agent.agentId) || builtinNames.has(base)) continue;
    if (!customByName.has(base)) customByName.set(base, agent);
  }
  const enriched = await Promise.all(Array.from(customByName.values()).map(enrichSessionAgent));
  // Built-ins first; custom agents follow in scan order (newest session
  // first). NewChatDialog's display-order sort is stable, so unranked
  // custom names keep this relative order.
  return [...builtins, ...enriched];
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
