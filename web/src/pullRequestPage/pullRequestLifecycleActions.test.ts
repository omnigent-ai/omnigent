import { afterEach, describe, expect, it, vi } from "vitest";

import {
  lifecycleActionState,
  lifecycleAvailability,
  resetLifecycleActionsForTests,
  subscribeLifecycleActions,
  updateLifecycleActionState,
} from "./pullRequestLifecycleActions";
import { stateChainKey } from "./pullRequestStateChain";

const KEY = stateChainKey("user-1", "https://github.com/acme/app/pull/42");

afterEach(() => {
  resetLifecycleActionsForTests();
});

describe("updateLifecycleActionState", () => {
  it("tells no one when the change leaves the state as it was", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeLifecycleActions(listener);
    // As a surface leaving a PR does when it had no confirmation open.
    updateLifecycleActionState(KEY, (current) => current);
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("tells its listeners about a change that does something", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeLifecycleActions(listener);
    updateLifecycleActionState(KEY, (current) => ({ ...current, running: "convert_to_draft" }));
    expect(listener).toHaveBeenCalledTimes(1);
    expect(lifecycleActionState(KEY).running).toBe("convert_to_draft");
    unsubscribe();
  });
});

describe("lifecycleAvailability", () => {
  const facts = {
    prUrl: "https://dev.azure.com/contoso/web/_git/app/pullrequest/7",
    state: "OPEN",
    isDraft: false,
    mergeable: false,
    mergesOnGitHub: false,
    headSha: "abc123",
    autoMergeOn: false,
    macMerge: null,
    offered: () => false,
    held: false,
  };

  it("links a change the page can't make to GitHub", () => {
    expect(lifecycleAvailability("convert_to_draft", facts)).toEqual({
      kind: "github",
      url: facts.prUrl,
    });
  });

  it("offers nothing for another provider's PR, which GitHub can't change", () => {
    const onAzure = { ...facts, onGitHub: false };
    expect(lifecycleAvailability("convert_to_draft", onAzure)).toEqual({ kind: "hidden" });
    expect(lifecycleAvailability("ready_for_review", { ...onAzure, isDraft: true })).toEqual({
      kind: "hidden",
    });
  });
});
