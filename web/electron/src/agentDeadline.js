// The deadline the agent browser tools' relay sends with each
// action (web/src/components/BrowserPane/agentTools): the time the agent stops
// waiting for it. Main stops the action there, whatever it is doing.

"use strict";

// For a call that brings no deadline (a web app from before them).
const DEFAULT_ACTION_MS = 20_000;

const PAGE_TIMEOUT_ERROR =
  "the page didn't respond before the action's time ran out. It may be paused at a breakpoint in DevTools; resume it and try again.";

/**
 * Whether an agent call is past the time its caller gave up on it
 * (`deadlineAt`, epoch ms). Past that, it does nothing: the agent was
 * already told it failed.
 *
 * @param {{ deadlineAt?: unknown } | undefined} opts The call's arguments or options.
 * @param {() => number} [now]
 * @returns {boolean}
 */
function pastAgentDeadline(opts, now = Date.now) {
  const deadlineAt = opts?.deadlineAt;
  return typeof deadlineAt === "number" && Number.isFinite(deadlineAt) && now() > deadlineAt;
}

/**
 * Split an action's parameters into the caller's deadline and the action's
 * own arguments, which are what browserIpc's checks see. Without one, the
 * action gets DEFAULT_ACTION_MS.
 *
 * @param {unknown} params
 * @param {() => number} [now]
 * @returns {{ deadlineAt: number, params: unknown }}
 */
function takeAgentDeadline(params, now = Date.now) {
  if (!params || typeof params !== "object" || Array.isArray(params) || !("deadlineAt" in params)) {
    return { deadlineAt: now() + DEFAULT_ACTION_MS, params };
  }
  const { deadlineAt, ...rest } = /** @type {Record<string, unknown>} */ (params);
  const valid = typeof deadlineAt === "number" && Number.isFinite(deadlineAt);
  return { deadlineAt: valid ? deadlineAt : now() + DEFAULT_ACTION_MS, params: rest };
}

/**
 * `work`'s result, or PAGE_TIMEOUT_ERROR at `deadlineAt` (none: just `work`).
 *
 * @template T
 * @param {Promise<T>} work
 * @param {number | undefined} deadlineAt
 * @returns {Promise<T>}
 */
async function beforeAgentDeadline(work, deadlineAt) {
  if (typeof deadlineAt !== "number" || !Number.isFinite(deadlineAt)) return work;
  // The deadline may win; the rejection work gets later is expected.
  work.catch?.(() => {});
  let timer;
  try {
    return await Promise.race([
      work,
      new Promise((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error(PAGE_TIMEOUT_ERROR)),
          Math.max(0, deadlineAt - Date.now()),
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

module.exports = {
  beforeAgentDeadline,
  DEFAULT_ACTION_MS,
  PAGE_TIMEOUT_ERROR,
  pastAgentDeadline,
  takeAgentDeadline,
};
