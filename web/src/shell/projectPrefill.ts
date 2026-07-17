import type { Host } from "@/hooks/useHosts";
import type { Conversation } from "@/hooks/useConversations";
import type { HostWorktree } from "@/hooks/useHostWorktrees";

type ProjectPrefillPhase = "host" | "workspace" | "branch" | "settled";

export interface ProjectPrefillState {
  /** Project this machine is seeding for; "" = plain visit (starts done). */
  project: string;
  /** Location track: host → workspace → branch → settled. */
  phase: ProjectPrefillPhase;
  /** Agent track, independent so a slow agents fetch can't hold up the
   *  location seeding (or the generic defaults gated on "settled"). */
  agentSeeded: boolean;
  /** Workspace the machine seeded, so the branch step can confirm it is still in place. */
  seededWorkspace: string | null;
}

export function initialPrefillState(project: string): ProjectPrefillState {
  const plain = project === "";
  return {
    project,
    phase: plain ? "settled" : "host",
    agentSeeded: plain,
    seededWorkspace: null,
  };
}

/** True once both tracks are done and stepping is a no-op. */
export function prefillDone(state: ProjectPrefillState): boolean {
  return state.phase === "settled" && state.agentSeeded;
}

interface ProjectPrefillInputs {
  /** Newest-session lookup: undefined = still loading, null = project has no sessions. */
  newest: Conversation | null | undefined;
  newestFailed: boolean;
  hosts: Host[] | undefined;
  /** Pickable agents; undefined = still loading. */
  agents: { id: string }[] | undefined;
  sandboxSelected: boolean;
  /** Live host pick; a mid-flight manual switch aborts the location seeding
   *  so another host's workspace path can't land in the field. */
  selectedHostId: string | null;
  /** Last-used agent id from localStorage (readLastAgentId()). */
  lastAgentId: string | null;
  /** Worktree set of the newest session's workspace (resolves a worktree-born
   *  session to its main repo). undefined = loading or not requested yet. */
  sourceWorktrees: HostWorktree[] | undefined;
  sourceWorktreesFailed: boolean;
  /** Live composer values the branch step checks before generating a name. */
  workspaceTrimmed: string;
  branchName: string;
  prefilledBranch: string;
  /** Worktree set of the CURRENT workspace (git-ness probe). undefined = loading/placeholder. */
  hostWorktrees: HostWorktree[] | undefined;
  hostWorktreesFailed: boolean;
}

interface ProjectPrefillWrites {
  hostId?: string;
  agentId?: string;
  workspace?: string;
  branch?: string;
}

/**
 * One transition. null = keep waiting for data; otherwise the next state
 * plus slot writes to apply (fill-empty-only; the component enforces that).
 * The agent seed and the location phases advance independently, mirroring
 * the data they wait on: agents can lag the hosts/session lookups.
 */
export function projectPrefillStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
): { state: ProjectPrefillState; writes: ProjectPrefillWrites } | null {
  const writes: ProjectPrefillWrites = {};
  let next = state;

  if (!state.agentSeeded) {
    const { newest, newestFailed, agents, hosts, lastAgentId } = inputs;
    const lookupDone = newest !== undefined || newestFailed;
    // A session on a gone/offline host is unusable as a template — every
    // slot falls back to generic, the agent included. Judging that needs
    // the host list, so wait for it (the location track does too).
    const needsHosts = newest != null && newest.host_id != null;
    if (lookupDone && agents !== undefined && (!needsHosts || hosts !== undefined)) {
      next = { ...next, agentSeeded: true };
      const hostId = newest?.host_id;
      const hostUsable =
        hostId == null || (hosts ?? []).some((h) => h.host_id === hostId && h.status === "online");
      const agentId = newest?.agent_id;
      if (hostUsable && agentId != null && agents.some((a) => a.id === agentId)) {
        writes.agentId = agentId;
      } else if (lastAgentId) {
        // A failed lookup, agent-less/unusable session, or retired agent
        // still seeds the last-used agent so the composer never sits
        // without one.
        writes.agentId = lastAgentId;
      }
    }
  }

  const location = locationStep(next, inputs, writes);
  if (location !== null) next = location;

  if (next === state) return null; // both tracks waiting on data
  return { state: next, writes };
}

/** The location track's terminal phase; the generic defaults resume from here. */
function settled(state: ProjectPrefillState): ProjectPrefillState {
  return { ...state, phase: "settled" };
}

/** Advance the host → workspace → branch → settled track, adding any slot
 *  write to `writes`. null = this track is waiting on data. */
function locationStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
  writes: ProjectPrefillWrites,
): ProjectPrefillState | null {
  if (state.phase === "host") {
    // Just the wait point: the workspace phase writes host and workspace
    // TOGETHER once the source repo resolves, so a failed resolution can't
    // leave a host seeded with a generic workspace (half a template).
    const { newest, newestFailed, hosts } = inputs;
    if ((newest === undefined && !newestFailed) || hosts === undefined) return null;
    return { ...state, phase: "workspace" };
  }

  if (state.phase === "workspace") {
    const { newest, hosts, sourceWorktrees, sourceWorktreesFailed, selectedHostId } = inputs;
    const hostId = newest?.host_id ?? null;
    const sourceWorkspace = newest?.workspace ?? null;
    if (
      newest == null ||
      hostId === null ||
      sourceWorkspace === null ||
      // The user picked the sandbox or a different host while the lookup was
      // in flight — this workspace belongs to the newest session's host.
      inputs.sandboxSelected ||
      (selectedHostId !== null && selectedHostId !== hostId) ||
      // Online only: the picker disables offline hosts, so seeding one would
      // set up a create that can only fail instead of falling back.
      !(hosts ?? []).some((h) => h.host_id === hostId && h.status === "online")
    ) {
      // Nothing seedable: empty project, sandbox-origin or host-less
      // session, or the host is gone or offline.
      return settled(state);
    }
    if (newest.git_branch == null) {
      writes.hostId = hostId;
      writes.workspace = sourceWorkspace;
      return { ...state, phase: "branch", seededWorkspace: sourceWorkspace };
    }
    if (sourceWorktreesFailed) return settled(state);
    if (sourceWorktrees === undefined) return null; // still resolving
    const main = sourceWorktrees.find((w) => w.is_main);
    if (main === undefined) return settled(state);
    writes.hostId = hostId;
    writes.workspace = main.path;
    return { ...state, phase: "branch", seededWorkspace: main.path };
  }

  if (state.phase === "branch") {
    const { workspaceTrimmed, hostWorktrees, hostWorktreesFailed, branchName, prefilledBranch } =
      inputs;
    // A sandbox create ignores workspace and branch, and its disabled
    // worktree query would leave this phase waiting forever.
    if (inputs.sandboxSelected) return settled(state);
    // The seeded repo is no longer in the field — leave the branch alone.
    if (workspaceTrimmed === "" || workspaceTrimmed !== state.seededWorkspace) {
      return settled(state);
    }
    // Settle on a failed listing rather than wait forever — a stuck machine
    // would keep the generic defaults gated off.
    if (hostWorktreesFailed) return settled(state);
    if (hostWorktrees === undefined) return null;
    if (!hostWorktrees.some((w) => w.is_main)) {
      return settled(state); // not a git repo
    }
    // Never clobber a typed branch or a worktree prefill.
    if (branchName === "" && prefilledBranch === "") {
      const suffix = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
      writes.branch = `worktree-${suffix}`;
    }
    return settled(state);
  }

  return null; // settled
}
