// Joins a session's own pull requests with the ones its sub-agents linked. Each PR appears once:
// the viewed session's own link wins, then the sub-agent that linked it first. The rest are
// listed as "Also in" on that row.

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { canonicalPullRequestUrl } from "@/lib/pullRequestReference";

/** A session a row can name: a sub-agent, by its id and the name the list shows for it. */
export interface SourceSession {
  id: string;
  name: string;
}

/** Where a row's PR came from: the sub-agent whose link wins it, and the others that link it too. */
export interface PullRequestSessionSource {
  /** The sub-agent whose link the row shows; null when the viewed session links it. */
  session: SourceSession | null;
  /** The other sessions linking the PR, in the order they would have won it. */
  alsoIn: SourceSession[];
}

/** One sub-agent in the viewed session's tree, with the PRs it has linked. */
export interface SubAgentPullRequestLinks {
  session: SourceSession;
  /** Levels below the viewed session: 1 for a direct sub-agent. */
  depth: number;
  prs: readonly PullRequestAssociation[];
}

export interface SessionPullRequestMerge {
  /** Each PR once: the viewed session's own first, then sub-agents' in the order they came. */
  prs: PullRequestAssociation[];
  /** The source of each row that has one, keyed by its association URL. */
  sources: ReadonlyMap<string, PullRequestSessionSource>;
}

interface Candidate {
  session: SourceSession;
  depth: number;
  association: PullRequestAssociation;
}

/**
 * Orders candidate links so the winner comes first: the earliest first sighting, a sighting the
 * server did not time last, then the shallower sub-agent, then the session id.
 */
function compareLinks(left: Candidate, right: Candidate): number {
  const leftSeen = left.association.first_seen_at;
  const rightSeen = right.association.first_seen_at;
  if (leftSeen != null && rightSeen != null && leftSeen !== rightSeen) return leftSeen - rightSeen;
  if (leftSeen != null && rightSeen == null) return -1;
  if (leftSeen == null && rightSeen != null) return 1;
  if (left.depth !== right.depth) return left.depth - right.depth;
  // Code-unit order, the same on every machine, unlike a locale's collation.
  return left.session.id < right.session.id ? -1 : left.session.id > right.session.id ? 1 : 0;
}

/**
 * The "Also in" note for a row, naming the other sessions that link its PR, or null when none do.
 *
 * @param alsoIn - The other sessions in their source order.
 */
export function alsoInNote(alsoIn: readonly SourceSession[]): string | null {
  if (alsoIn.length === 0) return null;
  const [first, ...rest] = alsoIn;
  if (rest.length === 0) return `Also in ${first!.name}`;
  if (rest.length === 1) return `Also in ${first!.name} and ${rest[0]!.name}`;
  return `Also in ${first!.name} and ${rest.length} more`;
}

/**
 * The viewed session's own PRs with its sub-agents' PRs joined in, each PR listed once.
 *
 * @param own - The viewed session's linked PRs, in display order.
 * @param members - The sub-agents to read, in the order their rows came.
 */
export function mergeSubAgentPullRequests(
  own: readonly PullRequestAssociation[],
  members: readonly SubAgentPullRequestLinks[],
): SessionPullRequestMerge {
  const ownKeys = new Set(own.map((pr) => canonicalPullRequestUrl(pr.url)));
  const candidatesByKey = new Map<string, Candidate[]>();
  for (const member of members) {
    for (const association of member.prs) {
      const key = canonicalPullRequestUrl(association.url);
      const list = candidatesByKey.get(key) ?? [];
      list.push({ session: member.session, depth: member.depth, association });
      candidatesByKey.set(key, list);
    }
  }
  const sources = new Map<string, PullRequestSessionSource>();
  const prs: PullRequestAssociation[] = [];
  const listed = new Set<string>();
  for (const pr of own) {
    const key = canonicalPullRequestUrl(pr.url);
    if (listed.has(key)) continue;
    listed.add(key);
    prs.push(pr);
    const linkers = [...(candidatesByKey.get(key) ?? [])].sort(compareLinks);
    if (linkers.length > 0) {
      sources.set(pr.url, { session: null, alsoIn: linkers.map((link) => link.session) });
    }
  }
  for (const member of members) {
    for (const association of member.prs) {
      const key = canonicalPullRequestUrl(association.url);
      if (ownKeys.has(key) || listed.has(key)) continue;
      listed.add(key);
      const [winner, ...others] = [...candidatesByKey.get(key)!].sort(compareLinks);
      prs.push(winner!.association);
      sources.set(winner!.association.url, {
        session: winner!.session,
        alsoIn: others.map((link) => link.session),
      });
    }
  }
  return { prs, sources };
}

/**
 * A limiter that runs at most `limit` tasks at once. A queued task whose signal aborts leaves the
 * queue without starting, so a list that goes away stops its queued reads.
 *
 * @param limit - The most tasks in flight, e.g. `4`.
 * @returns A function that runs a task once a slot is free, or rejects when `signal` aborts first.
 */
export function createConcurrencyLimit(
  limit: number,
): <Result>(task: () => Promise<Result>, signal?: AbortSignal) => Promise<Result> {
  let running = 0;
  const queue: { wake: () => void; cancel: (reason: unknown) => void }[] = [];
  const release = () => {
    const next = queue.shift();
    // The waiter takes over the finishing task's slot, so the count never exceeds the limit.
    if (next) next.wake();
    else running -= 1;
  };
  return async <Result>(task: () => Promise<Result>, signal?: AbortSignal): Promise<Result> => {
    if (signal?.aborted) throw abortReason(signal);
    if (running < limit) {
      running += 1;
    } else {
      await new Promise<void>((resolve, reject) => {
        const waiter = {
          wake: () => {
            signal?.removeEventListener("abort", onAbort);
            resolve();
          },
          cancel: reject,
        };
        const onAbort = () => {
          const index = queue.indexOf(waiter);
          if (index !== -1) queue.splice(index, 1);
          waiter.cancel(abortReason(signal!));
        };
        queue.push(waiter);
        signal?.addEventListener("abort", onAbort, { once: true });
      });
    }
    try {
      return await task();
    } finally {
      release();
    }
  };
}

function abortReason(signal: AbortSignal | undefined): unknown {
  return signal?.reason ?? new Error("Aborted");
}
