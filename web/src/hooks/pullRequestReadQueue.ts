// Paces the server reads of individual pull requests, so a session that links dozens of PRs
// can't use up the browser's concurrent requests to the server and stall sends and streams.

/** Background reads of individual pull requests in flight at once, across every session. */
const PULL_REQUEST_READ_CONCURRENCY = 2;

interface QueuedPullRequestRead {
  key: string;
  /** Whether the read itself was asked for by the user, such as an open details view's. */
  interactive: boolean;
  start: () => void;
}

/**
 * Runs reads of individual pull requests a few at a time, oldest first.
 *
 * The composer and the panel's list poll one read per linked PR, and each read asks the
 * host's git provider. Queued, those statuses fill in gradually.
 *
 * A prioritized PR, the one open in a details view or a read the user asked for, goes first
 * and may use one more slot, so it doesn't wait behind background reads. That slot is shared:
 * a read the user has moved away from keeps it until it settles, so moving between PRs faster
 * than they load can't add more requests.
 */
export class PullRequestReadQueue {
  private running = 0;
  private readonly waiting: QueuedPullRequestRead[] = [];
  private readonly prioritized = new Map<string, number>();

  async run<T>(
    key: string,
    signal: AbortSignal | undefined,
    read: () => Promise<T>,
    { interactive = false }: { interactive?: boolean } = {},
  ): Promise<T> {
    const readSignal = signal ?? new AbortController().signal;
    readSignal.throwIfAborted();
    const release = await this.waitForTurn(key, interactive, readSignal);
    try {
      // Closing a session frees its running reads' slots before it cancels the waiting ones,
      // so a read can be handed a slot just as it's cancelled; it must not start then.
      readSignal.throwIfAborted();
      return await read();
    } finally {
      release();
    }
  }

  /** Reads `key` ahead of the queue until the returned function is called. */
  prioritize(key: string): () => void {
    this.prioritized.set(key, (this.prioritized.get(key) ?? 0) + 1);
    this.drain();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const remaining = (this.prioritized.get(key) ?? 1) - 1;
      if (remaining > 0) this.prioritized.set(key, remaining);
      else this.prioritized.delete(key);
    };
  }

  /** Resolves, once `key` may start, with a function that frees its slot. */
  private waitForTurn(key: string, interactive: boolean, signal: AbortSignal): Promise<() => void> {
    return new Promise((resolve, reject) => {
      const entry: QueuedPullRequestRead = {
        key,
        interactive,
        start: () => {
          signal.removeEventListener("abort", abandon);
          this.running += 1;
          let holding = true;
          const release = () => {
            if (!holding) return;
            holding = false;
            signal.removeEventListener("abort", release);
            this.running -= 1;
            this.drain();
          };
          // A read that outlives its cancelled query must not keep its slot.
          signal.addEventListener("abort", release, { once: true });
          resolve(release);
        },
      };
      // A cancelled query, such as one whose session closed, gives up its place.
      const abandon = () => {
        const index = this.waiting.indexOf(entry);
        if (index >= 0) this.waiting.splice(index, 1);
        reject(signal.reason);
      };
      signal.addEventListener("abort", abandon, { once: true });
      this.waiting.push(entry);
      this.drain();
    });
  }

  private drain() {
    for (;;) {
      const viewed = this.waiting.findIndex(
        (entry) => entry.interactive || this.prioritized.has(entry.key),
      );
      if (viewed >= 0 && this.running < PULL_REQUEST_READ_CONCURRENCY + 1) {
        this.waiting.splice(viewed, 1)[0]!.start();
      } else if (this.waiting.length > 0 && this.running < PULL_REQUEST_READ_CONCURRENCY) {
        this.waiting.shift()!.start();
      } else {
        return;
      }
    }
  }
}

/** The one queue every session's server reads of individual pull requests share. */
export const pullRequestReads = new PullRequestReadQueue();

/** The queue's key for `prUrl` as `conversationId` reads it. */
export function pullRequestReadKey(conversationId: string, prUrl: string): string {
  return JSON.stringify([conversationId, prUrl]);
}
