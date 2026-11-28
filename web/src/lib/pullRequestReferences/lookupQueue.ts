// Bounds the reference lookups a page starts: a few at a time, and only so many in all, so a
// description that names hundreds of PRs can't spend a reader's GitHub budget on its own.

/** Lookups in flight at once, across every page. */
export const MAX_LOOKUPS_IN_FLIGHT = 4;
/** Distinct PRs one page looks up; references past it stay plain links. */
export const MAX_LOOKUPS_PER_PAGE = 60;

/** Why a waiting lookup didn't go out: the reads it would spend are held now. */
export class LookupHeldError extends Error {
  constructor() {
    super("Automatic reads are held for this reader.");
    this.name = "LookupHeldError";
  }
}

export interface LookupGates {
  /** Ends the wait, or stops the lookup before it's sent, once its view is gone. */
  signal?: AbortSignal;
  /** Asked again when the lookup's turn comes, since holds can start while it waits. */
  stillAllowed?: () => boolean;
}

export interface LookupQueue {
  /**
   * Runs `lookup` once fewer than the limit are in flight, unless `signal` aborts first or
   * `stillAllowed` says no when its turn comes ({@link LookupHeldError}).
   */
  run: <Result>(lookup: () => Promise<Result>, gates?: LookupGates) => Promise<Result>;
}

export function createLookupQueue(limit = MAX_LOOKUPS_IN_FLIGHT): LookupQueue {
  let running = 0;
  const waiting: (() => void)[] = [];
  // A finished lookup hands its slot straight to the next waiting one, so none slips in between.
  const release = () => {
    const next = waiting.shift();
    if (next) next();
    else running -= 1;
  };
  const acquire = (signal: AbortSignal | undefined) =>
    new Promise<void>((start, cancel) => {
      if (running < limit) {
        running += 1;
        start();
        return;
      }
      const take = () => {
        signal?.removeEventListener("abort", leave);
        start();
      };
      const leave = () => {
        const at = waiting.indexOf(take);
        if (at !== -1) waiting.splice(at, 1);
        cancel(signal!.reason);
      };
      waiting.push(take);
      signal?.addEventListener("abort", leave, { once: true });
    });
  return {
    async run(lookup, { signal, stillAllowed } = {}) {
      signal?.throwIfAborted();
      await acquire(signal);
      try {
        signal?.throwIfAborted();
        if (stillAllowed && !stillAllowed()) throw new LookupHeldError();
        return await lookup();
      } finally {
        release();
      }
    },
  };
}

/** The PRs one page has started looking up, up to its cap. */
export interface PageLookupBudget {
  /**
   * Whether the lookup of `key`, which reads `url`, may start: one already started, or a new one
   * under the cap. A page counts keys; a chat counts URLs.
   */
  admit: (key: string, url: string) => boolean;
}

export function createPageLookupBudget(cap = MAX_LOOKUPS_PER_PAGE): PageLookupBudget {
  const started = new Set<string>();
  return {
    admit(key) {
      if (started.has(key)) return true;
      if (started.size >= cap) return false;
      started.add(key);
      return true;
    },
  };
}

/** Shared by every page, so the limit holds however many pages are open. */
export const pullRequestReferenceLookups = createLookupQueue();
