// The deadline for one session read (a snapshot, items page or host lookup).
// Kept free of imports so the store and the identity chokepoint can share it,
// and so suites that mock either module still load it.

// A session read still unanswered after this counts as failed, so its caller
// retries instead of waiting on a request the server never answers.
export const SESSION_READ_TIMEOUT_MS = 15_000;

/** Run a session read, rejecting with a retryable timeout if the server never answers. */
export async function withReadTimeout<T>(
  read: (signal: AbortSignal) => Promise<T>,
  timeoutMs = SESSION_READ_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      const error = new DOMException("session read timed out", "TimeoutError");
      controller.abort(error);
      reject(error);
    }, timeoutMs);
  });
  try {
    // The race also bounds a read that is stuck before it reaches the signal.
    return await Promise.race([read(controller.signal), timedOut]);
  } finally {
    clearTimeout(timer);
  }
}
