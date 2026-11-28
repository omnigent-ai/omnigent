import { afterEach, describe, expect, it } from "vitest";

import { pullRequestChangeInFlight } from "./pullRequestChangeInFlight";
import {
  resetLifecycleActionsForTests,
  updateLifecycleActionState,
} from "./pullRequestLifecycleActions";
import {
  resetStateChainsForTests,
  setStateChainState,
  stateChainKey,
} from "./pullRequestStateChain";

const KEY = stateChainKey("user-1", "https://github.com/acme/app/pull/42");

afterEach(() => {
  resetStateChainsForTests();
  resetLifecycleActionsForTests();
});

describe("pullRequestChangeInFlight", () => {
  it("is null while nothing runs, and once a run settles to a notice", () => {
    expect(pullRequestChangeInFlight(KEY)).toBeNull();
    setStateChainState(KEY, {
      running: null,
      notice: {
        operation: "pull_request_close",
        attempt: "attempt-test",
        outcome: { kind: "refused", message: "No", commentPosted: false },
        boundTo: {} as never,
      },
    });
    expect(pullRequestChangeInFlight(KEY)).toBeNull();
  });

  it("names the close or reopen running, from whichever surface started it", () => {
    setStateChainState(KEY, { running: "pull_request_reopen", notice: null });
    expect(pullRequestChangeInFlight(KEY)).toEqual({
      operation: "pull_request_reopen",
      label: "Reopening…",
    });
  });

  it("names a draft, ready or auto-merge change running", () => {
    for (const [running, label] of [
      ["convert_to_draft", "Converting…"],
      ["ready_for_review", "Marking ready…"],
      ["enable_auto_merge", "Turning on auto-merge…"],
    ] as const) {
      updateLifecycleActionState(KEY, (current) => ({ ...current, running }));
      expect(pullRequestChangeInFlight(KEY)?.label).toBe(label);
    }
  });

  it("is per workspace user and pull request", () => {
    setStateChainState(KEY, { running: "pull_request_close", notice: null });
    const other = stateChainKey("user-2", "https://github.com/acme/app/pull/42");
    expect(pullRequestChangeInFlight(other)).toBeNull();
  });
});
