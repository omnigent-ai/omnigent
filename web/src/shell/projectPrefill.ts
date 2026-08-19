import type { Host } from "@/hooks/useHosts";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";

type ProjectPrefillPhase = "location" | "settled";

/**
 * A project's stored session defaults, as the composer consumes them. This is
 * the ONLY project-driven prefill source: a set field seeds the composer, and
 * an absent field falls through to the composer's generic defaults (last host /
 * recent workspace / last-used agent).
 *
 * `undefined` means the config is still loading for a project that has one — the
 * machine WAITS in that case so a generic default can't win the race. Pass an
 * empty object `{}` for "no config / nothing to wait for" (a label-only folder
 * with no first-class row, or a genuinely empty config) so the machine settles
 * immediately and the generic defaults take over.
 */
export interface ProjectPrefillConfig {
  hostId?: string;
  workspace?: string;
  agentId?: string;
  /** Opt-in worktree default; only `true` is meaningful (absent = no worktree). */
  useWorktree?: boolean;
}

export interface ProjectPrefillState {
  /** Project this machine is seeding for; "" = plain visit (starts done). */
  project: string;
  /** Location track: seed host/workspace from config, then settle. */
  phase: ProjectPrefillPhase;
  /** Agent track, independent so a slow agents fetch can't hold up the
   *  location seeding (or the generic defaults gated on "settled"). */
  agentSeeded: boolean;
}

export function initialPrefillState(project: string): ProjectPrefillState {
  const plain = project === "";
  return {
    project,
    phase: plain ? "settled" : "location",
    agentSeeded: plain,
  };
}

/** True once both tracks are done and stepping is a no-op. */
export function prefillDone(state: ProjectPrefillState): boolean {
  return state.phase === "settled" && state.agentSeeded;
}

interface ProjectPrefillInputs {
  hosts: Host[] | undefined;
  /** Pickable agents; undefined = still loading. */
  agents: { id: string }[] | undefined;
  sandboxSelected: boolean;
  /** Whether the server offers managed sandbox hosts. Gates seeding a stored
   *  sandbox default — an OSS server that no longer offers it drops the hint. */
  managedSandboxesEnabled: boolean;
  /** Live host pick; a mid-flight manual switch aborts the config host seeding
   *  so a stored host can't clobber the user's own choice. */
  selectedHostId: string | null;
  /** Last-used agent id from localStorage (readLastAgentId()) — the generic
   *  agent fallback when the config sets none. */
  lastAgentId: string | null;
  /** Stored project defaults (the prefill source). undefined = still loading
   *  (wait); {} = no config / nothing to wait for (settle to generic defaults). */
  config: ProjectPrefillConfig | undefined;
}

interface ProjectPrefillWrites {
  hostId?: string;
  agentId?: string;
  workspace?: string;
  /** Select the managed sandbox as the target (config.hostId was the sandbox
   *  sentinel). Distinct from `hostId` — the sandbox is not a real host id. */
  selectSandbox?: boolean;
}

/**
 * Something the composer must SAY rather than silently do. Today's only case:
 * the project's configured host isn't online, so its workspace is dropped and
 * the generic defaults bind the session to whatever repo was used last — a
 * silent rebinding the user would otherwise only discover after the fact.
 */
export interface ProjectPrefillNotice {
  kind: "host-offline";
  hostId: string;
}

/** Mutable sink for a step's outputs, so both tracks can add to it. */
interface StepOutput {
  writes: ProjectPrefillWrites;
  notice?: ProjectPrefillNotice;
}

/**
 * One transition. null = keep waiting for data; otherwise the next state
 * plus slot writes to apply (fill-empty-only; the component enforces that).
 * The agent seed and the location phase advance independently, mirroring
 * the data they wait on: agents can lag the host list.
 */
export function projectPrefillStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
): {
  state: ProjectPrefillState;
  writes: ProjectPrefillWrites;
  notice?: ProjectPrefillNotice;
} | null {
  const out: StepOutput = { writes: {} };
  const writes = out.writes;
  let next = state;

  // Config still loading for a project that has one — wait so a generic default
  // can't win the race against a stored default.
  if (inputs.config === undefined) return null;

  if (!state.agentSeeded) {
    const { agents, lastAgentId, config } = inputs;
    // Need the pickable agents to judge whether a stored / last-used agent is
    // still selectable; wait for them.
    if (agents !== undefined) {
      next = { ...next, agentSeeded: true };
      if (config?.agentId != null && agents.some((a) => a.id === config.agentId)) {
        writes.agentId = config.agentId;
      } else if (lastAgentId && agents.some((a) => a.id === lastAgentId)) {
        // No configured agent (or it's no longer available) — seed the
        // last-used agent so the composer never sits without one.
        writes.agentId = lastAgentId;
      }
    }
  }

  const location = locationStep(next, inputs, out);
  if (location !== null) next = location;

  if (next === state) return null; // both tracks waiting on data
  return { state: next, writes, notice: out.notice };
}

/** The location track's terminal phase; the generic defaults resume from here. */
function settled(state: ProjectPrefillState): ProjectPrefillState {
  return { ...state, phase: "settled" };
}

/** Seed host (+ workspace) from config, then settle. null = waiting on data. */
function locationStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
  out: StepOutput,
): ProjectPrefillState | null {
  if (state.phase !== "location") return null;
  const { hosts, selectedHostId, config } = inputs;
  const writes = out.writes;

  // A stored sandbox default: select the sandbox (gated on the server still
  // offering it), unless the user already picked a host / the sandbox. The
  // sandbox sentinel is never a real host id, so it's handled before the
  // online-host lookup below. Mirrors the composer's last-choice sandbox path.
  if (config?.hostId === SANDBOX_HOST_CHOICE) {
    if (inputs.managedSandboxesEnabled && !inputs.sandboxSelected && selectedHostId === null) {
      writes.selectSandbox = true;
    }
    return settled(state);
  }

  // The online check needs the host list — wait for it.
  if (hosts === undefined) return null;

  // Seed the configured host only when it's online (dropped otherwise — with a
  // notice, below — so an offline default can't set up a create that only
  // fails), and only when the user hasn't already picked a host / the sandbox.
  const configHostOnline =
    config?.hostId != null &&
    hosts.some((h) => h.host_id === config.hostId && h.status === "online");
  const configHostUsable =
    configHostOnline &&
    !inputs.sandboxSelected &&
    (selectedHostId === null || selectedHostId === config!.hostId);
  // Configured host present but not online: nothing is seeded, so the generic
  // defaults will bind this session to the last-used host and repo. Say so —
  // an unannounced rebinding is how a session ends up filed under one project
  // while running in another project's checkout.
  if (
    config?.hostId != null &&
    !configHostOnline &&
    !inputs.sandboxSelected &&
    selectedHostId === null
  ) {
    out.notice = { kind: "host-offline", hostId: config.hostId };
  }
  if (configHostUsable) {
    writes.hostId = config!.hostId;
    // A configured workspace seeds the field; without one, the composer's
    // home-fallback fills it. Any opt-in worktree (config.useWorktree) is
    // handled by a dedicated post-settle effect once the workspace is in place
    // (see NewChatDialog's worktree effect).
    if (config!.workspace != null) writes.workspace = config!.workspace;
  }
  return settled(state);
}
