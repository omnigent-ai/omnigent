/**
 * Best-of-N: start one task on several agents at once, each in its own git
 * worktree, then keep one attempt and archive the rest.
 *
 * Attempts are ordinary sessions created as nested agents of the session the
 * run started from, so the sidebar groups them with it. Two labels tie the
 * attempts together: the group id, and the full list of attempt ids that
 * "Keep this attempt" archives from.
 */
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { nativeDisplayNameForAgent, nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import { randomUUID } from "@/lib/randomUUID";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { ApiError, createSession, postEvent, updateSession } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { splitWorktreePath } from "@/lib/worktreePaths";

export const BEST_OF_N_MIN_ATTEMPTS = 2;
/** The session grid shows at most four sessions. */
export const BEST_OF_N_MAX_ATTEMPTS = 4;
export const BEST_OF_N_GROUP_LABEL = "omnigent.best_of_n.group";
export const BEST_OF_N_ATTEMPTS_LABEL = "omnigent.best_of_n.attempts";

/**
 * The repository new attempt worktrees branch from: the source repository of
 * a server-created worktree, else the workspace itself.
 *
 * @param workspace - A session's workspace, e.g. "/Users/a/proj-worktrees/fix".
 * @returns e.g. "/Users/a/proj".
 */
export function bestOfNRepository(workspace: string): string {
  const trimmed = workspace.replace(/\/+$/, "") || workspace;
  return splitWorktreePath(trimmed)?.repo ?? trimmed;
}

function branchSlug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24)
    .replace(/-+$/, "");
}

/**
 * The worktree branch for one attempt, e.g. "best-of-n-1a2b3c4d-2-codex".
 *
 * @param groupId - The run's group id.
 * @param index - Zero-based attempt index.
 * @param agentLabel - The attempt's agent name, for readability.
 */
export function bestOfNBranchName(groupId: string, index: number, agentLabel: string): string {
  const slug = branchSlug(agentLabel);
  return `best-of-n-${groupId}-${index + 1}${slug ? `-${slug}` : ""}`;
}

/** The ids of every attempt in a session's best-of-N run, itself included. */
export function bestOfNAttemptIds(labels: Record<string, string> | null | undefined): string[] {
  const raw = labels?.[BEST_OF_N_ATTEMPTS_LABEL];
  if (!raw || !labels?.[BEST_OF_N_GROUP_LABEL]) return [];
  return [...new Set(raw.split(",").map((id) => id.trim()))].filter(Boolean);
}

/** The other attempts "Keep this attempt" archives; empty when the session is not an attempt. */
export function otherBestOfNAttemptIds(
  sessionId: string,
  labels: Record<string, string> | null | undefined,
): string[] {
  const ids = bestOfNAttemptIds(labels);
  return ids.includes(sessionId) ? ids.filter((id) => id !== sessionId) : [];
}

export interface BestOfNLaunchInput {
  /** The owned session the run starts from; attempts join its family. */
  sourceSessionId: string;
  hostId: string;
  /** Repository each attempt's worktree is created from. */
  repository: string;
  /** Ref every worktree branches from; blank uses the repository's current HEAD. */
  baseBranch?: string;
  prompt: string;
  /** One agent per attempt; the same agent may appear more than once. */
  agents: readonly AvailableAgent[];
}

export interface BestOfNAttemptFailure {
  agentName: string;
  message: string;
}

export interface BestOfNLaunchResult {
  groupId: string;
  /** Created attempts, in the order the agents were given. */
  sessionIds: string[];
  /** Attempts that were not created, or whose task was not delivered. */
  failures: BestOfNAttemptFailure[];
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Unknown error";
}

/**
 * Create one worktree session per agent, link them as one run, and send each
 * the same task. Attempts are independent: one failing doesn't stop the rest.
 */
export async function launchBestOfN(input: BestOfNLaunchInput): Promise<BestOfNLaunchResult> {
  const groupId = randomUUID().replace(/-/g, "").slice(0, 8);
  const baseBranch = input.baseBranch?.trim() || undefined;
  const names = input.agents.map((agent) => agent.display_name || nativeDisplayNameForAgent(agent));
  const failures: BestOfNAttemptFailure[] = [];
  const attempts: { id: string; agentName: string }[] = [];
  // One at a time: concurrent `git worktree add` runs in one repository can
  // race on its config lock.
  /* oxlint-disable no-await-in-loop */
  for (const [index, agent] of input.agents.entries()) {
    try {
      const session = await createSession(agent.id, [], {
        siblingSourceId: input.sourceSessionId,
        hostId: input.hostId,
        workspace: input.repository,
        git: { branchName: bestOfNBranchName(groupId, index, names[index]), baseBranch },
        title: `Attempt ${index + 1} · ${names[index]}`,
        labels: { ...nativeWrapperLabelsForAgent(agent), [BEST_OF_N_GROUP_LABEL]: groupId },
      });
      attempts.push({ id: session.id, agentName: names[index] });
    } catch (error) {
      failures.push({ agentName: names[index], message: errorMessage(error) });
    }
  }
  /* oxlint-enable no-await-in-loop */
  const sessionIds = attempts.map((attempt) => attempt.id);
  const attemptsLabel = sessionIds.join(",");
  // Link every attempt to the others and send each the task, all in parallel.
  const steps = await Promise.all(
    attempts.map(({ id }) =>
      Promise.allSettled([
        postEvent(id, {
          type: "message",
          data: {
            role: "user",
            content: [{ type: "input_text", text: input.prompt }],
            stable_id: randomUUID().replace(/-/g, ""),
          },
        }),
        updateSession(id, { labels: { [BEST_OF_N_ATTEMPTS_LABEL]: attemptsLabel } }),
      ]),
    ),
  );
  steps.forEach(([sent, linked], index) => {
    const { agentName } = attempts[index];
    if (sent.status === "rejected")
      failures.push({ agentName, message: `The task wasn't sent: ${errorMessage(sent.reason)}` });
    // A policy-denied task resolves without starting a turn; the attempt stays.
    else if (sent.value.denied)
      failures.push({ agentName, message: "A policy blocked the task, so no turn started." });
    if (linked.status === "rejected")
      failures.push({
        agentName,
        message: `It isn't linked to the other attempts: ${errorMessage(linked.reason)}`,
      });
  });
  return { groupId, sessionIds, failures };
}

export interface VerifiedAttempt {
  id: string;
  title: string;
}

export interface VerifiedAttempts {
  /** Owned, active attempts of the same run, safe to archive. */
  attempts: VerifiedAttempt[];
  /** Listed attempts whose lookup failed for another reason than missing or forbidden. */
  unresolved: string[];
}

/** A lookup that proves the session is gone or not the viewer's, not a passing failure. */
function confirmsExclusion(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

/** A run's group id, as `launchBestOfN` mints it. */
const RUN_GROUP = /^[0-9a-f]{8}$/;

/**
 * The other attempts "Keep this attempt" may archive: owned, active sessions of
 * the kept attempt's run. Labels are editable by anyone with edit access, so
 * membership also needs evidence editors can't change: the worktree branch the
 * server recorded when the session was created (`best-of-n-<group>-…`) and the
 * server-assigned family, both matching the kept attempt's. Lookups that fail
 * for another reason than missing or forbidden are reported as unresolved, so
 * a caller never treats a partial list as the whole run.
 *
 * @param keptId - The attempt being kept.
 * @param keptLabels - Its labels, naming the run's group and attempts.
 * @param fetchSession - Reads one session, e.g. `getSessionSlim`.
 */
export async function verifiedOtherAttempts(
  keptId: string,
  keptLabels: Record<string, string> | null | undefined,
  fetchSession: (id: string) => Promise<Session>,
): Promise<VerifiedAttempts> {
  const group = keptLabels?.[BEST_OF_N_GROUP_LABEL];
  const ids = otherBestOfNAttemptIds(keptId, keptLabels);
  const verified: VerifiedAttempts = { attempts: [], unresolved: [] };
  if (!group || !RUN_GROUP.test(group) || ids.length === 0) return verified;
  const [keptResult, ...sessions] = await Promise.allSettled([
    fetchSession(keptId),
    ...ids.map((id) => fetchSession(id)),
  ]);
  if (keptResult.status === "rejected") {
    if (!confirmsExclusion(keptResult.reason)) verified.unresolved.push(...ids);
    return verified;
  }
  const family = keptResult.value.family?.id;
  const branchPrefix = `best-of-n-${group}-`;
  const inRun = (session: Session) =>
    !!family &&
    session.family?.id === family &&
    session.gitBranch?.startsWith(branchPrefix) === true &&
    session.labels?.[BEST_OF_N_GROUP_LABEL] === group;
  if (keptResult.value.id !== keptId || !inRun(keptResult.value)) return verified;
  sessions.forEach((result, index) => {
    if (result.status === "rejected") {
      if (!confirmsExclusion(result.reason)) verified.unresolved.push(ids[index]);
      return;
    }
    const session = result.value;
    const sameRun =
      session.id === ids[index] &&
      inRun(session) &&
      bestOfNAttemptIds(session.labels).includes(keptId);
    if (sameRun && session.archived !== true && isOwnerLevel(session.permissionLevel ?? null))
      verified.attempts.push({ id: session.id, title: session.title || session.id });
  });
  return verified;
}
