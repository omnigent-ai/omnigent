// What a session's chat looked up for its PR references, for as long as the session is open. A
// chat keeps growing and its rows come and go as it scrolls, so each PR goes out once, a failure
// once more, and what came back stands in for any later read of it.

import type { QueryClient } from "@tanstack/react-query";

import { MAX_LOOKUPS_PER_PAGE, type PageLookupBudget } from "./lookupQueue";
import { keepsCachedReference, type ReferenceLookup } from "./useReferencedPullRequest";

/** How long a failed lookup waits before its one retry. */
export const FAILED_RETRY_MS = 30_000;
/** Lookups one reference sends in a view, as one account: its first, and a failure's retry. */
export const MAX_ATTEMPTS_PER_REFERENCE = 2;

/** A view's answers, by reference key, so a later read of one is never sent. */
export interface ReferenceLookupRecord {
  /** The answer the view has for `key`, shown while it isn't looked up. */
  remembered: (key: string) => ReferenceLookup | undefined;
  /** The answer that stands in for `key`'s lookup at `now`; undefined when it may go out. */
  insteadOfLookup: (key: string, now: number) => ReferenceLookup | undefined;
  /** Notes that `key`'s lookup went out. */
  sent: (key: string) => void;
  /**
   * Keeps an answer for `key`, a lookup's or one the shared cache settled on, unless the record
   * has a newer one: the newer read wins, and a failure never replaces what a read did say.
   */
  answered: (key: string, answer: ReferenceLookup) => void;
  /** Whether the view has read, or been told of, `key`'s PR. */
  tracks: (key: string) => boolean;
  /** Notes that the view shows `key`'s PR, however its answer came, so later ones reach it. */
  watch: (key: string) => void;
}

export interface SessionReferenceLookups {
  budget: PageLookupBudget;
  record: ReferenceLookupRecord;
}

/**
 * A new session view's record, with one cap over the PRs it looks up, by URL: one that's read
 * again as another account takes no more of it, but has its own key, and so its own answers.
 */
export function createSessionReferenceLookups(cap = MAX_LOOKUPS_PER_PAGE): SessionReferenceLookups {
  const urls = new Set<string>();
  const entries = new Map<string, { attempts: number; answer?: ReferenceLookup }>();
  return {
    budget: {
      admit(_key, url) {
        if (urls.has(url)) return true;
        if (urls.size >= cap) return false;
        urls.add(url);
        return true;
      },
    },
    record: {
      remembered: (key) => entries.get(key)?.answer,
      insteadOfLookup(key, now) {
        const entry = entries.get(key);
        if (!entry) return undefined;
        // What a read said stands in, whoever read it.
        if (entry.answer && entry.answer.kind !== "failed") return entry.answer;
        if (entry.attempts === 0) return undefined;
        const answer = entry.answer ?? { kind: "failed", readAt: 0 };
        if (entry.attempts >= MAX_ATTEMPTS_PER_REFERENCE) return answer;
        return now - answer.readAt >= FAILED_RETRY_MS ? undefined : answer;
      },
      sent(key) {
        const entry = entries.get(key);
        if (entry) entry.attempts += 1;
        else entries.set(key, { attempts: 1 });
      },
      tracks: (key) => entries.has(key),
      watch(key) {
        if (!entries.has(key)) entries.set(key, { attempts: 0 });
      },
      answered(key, answer) {
        const entry = entries.get(key) ?? { attempts: 0 };
        if (keepsCachedReference(entry.answer, answer)) return;
        entry.answer = answer;
        entries.set(key, entry);
      },
    },
  };
}

/**
 * Keeps `record` told of every answer the shared cache settles on for a PR the view has read,
 * whoever wrote it, so one that arrives while the view's row for it is scrolled away isn't lost
 * once the cache lets the query go. Returns the stop.
 */
export function followSharedReferenceAnswers(
  queryClient: QueryClient,
  record: ReferenceLookupRecord,
): () => void {
  return queryClient.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" || event.action.type !== "success") return;
    const key = JSON.stringify(event.query.queryKey);
    const answer = event.query.state.data as ReferenceLookup | undefined;
    if (answer && record.tracks(key)) record.answered(key, answer);
  });
}
