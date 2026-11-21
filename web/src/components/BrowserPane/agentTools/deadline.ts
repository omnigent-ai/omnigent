// The agent browser tools' time limit. Imports nothing.

// From the claim: the server waits 30 s for the result after a window claims
// the action (_BROWSER_ACTION_AWAIT_S), so everything the action does ends
// inside this. The desktop takes it with each call and stops there.
export const AGENT_ACTION_BUDGET_MS = 28_000;
export const AGENT_ACTION_EXPIRED_ERROR =
  "the browser action ran out of time before it could start; nothing was done";

/** The deadline for an action whose claim is about to be sent. The server
 *  answers the agent a fixed time after it takes the claim, which is after
 *  the request leaves here, so counting from now ends inside its wait
 *  however long the claim's response takes to arrive. */
export function agentActionDeadlineBeforeClaim(): number {
  return Date.now() + AGENT_ACTION_BUDGET_MS;
}
