// Pull-request lookups for the cards on the active canvas. Each lookup asks
// the session's runner (through the GitHub panel's query cache), so a bounded
// queue keeps a large canvas from firing dozens of runner requests at once.

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "@/hooks/useConversations";
import { fetchGithubInfo } from "@/hooks/useGithub";

export const PULL_REQUEST_REFRESH_MS = 300_000;
/** A failed lookup is retried this soon instead of waiting the full refresh window. */
export const PULL_REQUEST_RETRY_MS = 60_000;
export const PULL_REQUEST_CONCURRENCY = 4;
/** Matches the GitHub panel so the runner is asked at most every 30s per session. */
const GITHUB_INFO_STALE_MS = 30_000;

export interface CanvasPullRequest {
  number: number;
  title: string;
  /** "OPEN" | "MERGED" | "CLOSED" as reported by gh. */
  state: string;
  url: string;
}

export type CanvasPullRequests = Record<string, CanvasPullRequest | null>;

interface PullRequestTask {
  key: string;
  run: () => Promise<void>;
}

export class PullRequestQueue {
  private readonly active = new Set<string>();
  private readonly pending = new Map<string, () => Promise<void>>();
  private readonly concurrency: number;

  constructor(concurrency: number) {
    this.concurrency = concurrency;
  }

  replace(tasks: PullRequestTask[]): void {
    this.pending.clear();
    for (const task of tasks) {
      if (!this.active.has(task.key)) this.pending.set(task.key, task.run);
    }
    this.drain();
  }

  clear(): void {
    this.pending.clear();
  }

  private drain(): void {
    while (this.active.size < this.concurrency && this.pending.size > 0) {
      const entry = this.pending.entries().next().value as
        [string, () => Promise<void>] | undefined;
      if (!entry) return;
      const [key, run] = entry;
      this.pending.delete(key);
      this.active.add(key);
      void run()
        .catch(() => undefined)
        .finally(() => {
          this.active.delete(key);
          this.drain();
        });
    }
  }
}

function samePullRequest(
  left: CanvasPullRequest | null | undefined,
  right: CanvasPullRequest | null,
): boolean {
  if (left === undefined) return false;
  if (left === null || right === null) return left === right;
  return (
    left.number === right.number &&
    left.title === right.title &&
    left.state === right.state &&
    left.url === right.url
  );
}

/**
 * The open pull request (if any) for each branch-bearing session in
 * ``sessions``, refreshed at most every five minutes per session (a failed
 * lookup retries after one minute). Pass a memoized list — the queue is
 * rebuilt whenever it changes, and re-checked on a timer in between.
 */
export function usePullRequests(sessions: readonly Conversation[]): CanvasPullRequests {
  const queryClient = useQueryClient();
  const [pullRequests, setPullRequests] = useState<CanvasPullRequests>({});
  // Bumped on a timer so lookups fall due even while the session list is unchanged.
  const [tick, setTick] = useState(0);
  const checkedAtRef = useRef<Record<string, number>>({});
  const queueRef = useRef<PullRequestQueue | null>(null);
  queueRef.current ??= new PullRequestQueue(PULL_REQUEST_CONCURRENCY);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    const timer = setInterval(() => setTick((value) => value + 1), PULL_REQUEST_RETRY_MS);
    return () => {
      aliveRef.current = false;
      clearInterval(timer);
      queueRef.current?.clear();
    };
  }, []);

  useEffect(() => {
    const queue = queueRef.current;
    if (!queue) return;
    const now = Date.now();
    const due = sessions.filter(
      (session) =>
        Boolean(session.git_branch?.trim()) &&
        now - (checkedAtRef.current[session.id] ?? 0) >= PULL_REQUEST_REFRESH_MS,
    );
    queue.replace(
      due.map((session) => ({
        key: session.id,
        run: async () => {
          // Claim the slot so a rebuilt queue does not re-enqueue an in-flight lookup;
          // a failure below shortens the wait to the retry window.
          checkedAtRef.current[session.id] = Date.now();
          let info;
          try {
            info = await queryClient.fetchQuery({
              queryKey: ["github-info", session.id],
              queryFn: () => fetchGithubInfo(session.id),
              staleTime: GITHUB_INFO_STALE_MS,
            });
          } catch (error) {
            checkedAtRef.current[session.id] =
              Date.now() - PULL_REQUEST_REFRESH_MS + PULL_REQUEST_RETRY_MS;
            throw error;
          }
          if (!aliveRef.current) return;
          const pr = info.available ? info.pr : null;
          const next: CanvasPullRequest | null =
            pr && /^https:\/\//.test(pr.url)
              ? { number: pr.number, title: pr.title, state: pr.state, url: pr.url }
              : null;
          setPullRequests((current) =>
            samePullRequest(current[session.id], next)
              ? current
              : { ...current, [session.id]: next },
          );
        },
      })),
    );
    return () => queue.clear();
  }, [queryClient, sessions, tick]);

  return pullRequests;
}
