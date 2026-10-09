import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";

const statePath = process.env.SETUP_UV_TEST_STATE;
const state = JSON.parse(readFileSync(statePath, "utf8"));
const attempt = Number(process.env.STATE_attempt);
state.posts.push(attempt);
writeFileSync(statePath, JSON.stringify(state));

// Post actions run in reverse order, so attempt 1 observes the final count.
if (attempt === 1) {
  assert.equal(state.posts.length, state.attempts.length);
  assert.deepEqual(state.posts, [...state.posts].sort((a, b) => b - a));
}
