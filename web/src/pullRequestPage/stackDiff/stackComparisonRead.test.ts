import { afterEach, describe, expect, it } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type { StackMember } from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";
import { pullRequestStackView } from "@/pullRequestPage/usePullRequestStack";

import {
  stackComparisonPlan,
  stackComparisonQueryOptions,
  stackDiffAccount,
  storedStackDiffIntent,
} from "./stackComparisonRead";
import { stackDiffIntentMemory } from "./stackDiffIntent";
import type { StackDiffIntent } from "./stackDiffMode";

const URL_102 = "https://git.example.com/acme/app/pull/102";
const TARGET: CodeHostPullRequest = {
  provider: "github",
  host: "git.example.com",
  repository: "acme/app",
  number: 102,
  url: URL_102,
};
const PR = {
  url: URL_102,
  body: "Stacked on #100",
  head_sha: "c".repeat(40),
  base_sha: "d".repeat(40),
  base_ref: "main",
};

const node = (number: number, parentIndex: number | null): StackNode => ({
  depth: parentIndex === null ? 0 : 1,
  parentIndex,
  branch: `stack/${number}`,
  ref: {
    url: `https://git.example.com/acme/app/pull/${number}`,
    host: "git.example.com",
    repository: "acme/app",
    number,
  },
  external: false,
  markers: { current: false, merged: false },
});

/** PR 102 stacked on PR 100, its parent read at `parent`'s head and branch. */
function stackOf(parent: Partial<StackMember> = {}, nativeRevision: string | null = null) {
  const parse = {
    format: "test",
    trust: "verified",
    span: { start: 0, end: 0 },
    tolerances: [],
    nodes: [node(100, null), node(102, 0)],
    dependency: null,
    selfIndex: 1,
  } as unknown as StackParse;
  const member: StackMember = {
    url: "https://git.example.com/acme/app/pull/100",
    number: 100,
    title: "PR 100",
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: "stack/100",
    head_sha: "a".repeat(40),
    base_ref: "main",
    base_sha: "d".repeat(40),
    head_repository: "acme/app",
    base_repository: "acme/app",
    ...parent,
  };
  const view = pullRequestStackView(
    parse,
    { kind: "loaded", answer: { viewer: "alice", members: [member], missing: [], unread: [] } },
    () => false,
  );
  return { ...view, nativeRevision };
}

const planFor = ({
  pr = PR,
  stack = stackOf(),
  intent = "automatic" as StackDiffIntent,
  automaticPolicy = true,
  conversationId = "s1",
  target = TARGET as CodeHostPullRequest | null,
} = {}) => stackComparisonPlan({ pr, stack, target, conversationId, intent, automaticPolicy });

const keyOf = (plan: ReturnType<typeof planFor>) =>
  JSON.stringify(stackComparisonQueryOptions("s1", URL_102, plan.identity).queryKey);

afterEach(() => {
  window.localStorage.clear();
  stackDiffIntentMemory.resetUnsaved();
});

describe("stackComparisonPlan", () => {
  it("names another comparison for a push, a parent push, a retarget, an edit or a new stack", () => {
    const base = keyOf(planFor());
    expect(keyOf(planFor())).toBe(base);
    const changed = [
      planFor({ pr: { ...PR, head_sha: "e".repeat(40) } }),
      planFor({ stack: stackOf({ head_sha: "b".repeat(40) }) }),
      planFor({ pr: { ...PR, base_sha: "f".repeat(40) } }),
      planFor({ pr: { ...PR, body: "Stacked on #100, edited" } }),
      planFor({ stack: stackOf({}, "native-2") }),
      planFor({ conversationId: "s2" }),
    ];
    for (const plan of changed) expect(keyOf(plan)).not.toBe(base);
  });

  it("wants a comparison as its view would: Automatic where the base isn't the parent, or This PR", () => {
    // Based on main, not the parent's branch: Automatic compares.
    expect(planFor().wanted).toBe(true);
    // Based on the parent's branch already: all its changes are its own.
    const onParent = { ...PR, base_ref: "stack/100" };
    expect(planFor({ pr: onParent }).wanted).toBe(false);
    expect(planFor({ pr: onParent, intent: "this-pr" }).wanted).toBe(true);
    expect(planFor({ intent: "all" }).wanted).toBe(false);
    expect(planFor({ automaticPolicy: false }).wanted).toBe(false);
    expect(planFor({ automaticPolicy: false, intent: "this-pr" }).wanted).toBe(true);
  });

  it("has nothing to compare without a parent's head, or where the host can't compare", () => {
    expect(planFor({ stack: stackOf({ head_sha: null }) }).identity).toBeNull();
    expect(planFor({ target: null })).toMatchObject({ offered: false, identity: null });
    expect(planFor({ conversationId: "" })).toMatchObject({ offered: false, identity: null });
  });
});

describe("storedStackDiffIntent", () => {
  it("reads the code host's own choice for the PR, Automatic otherwise", () => {
    const account = stackDiffAccount(TARGET)!;
    expect(storedStackDiffIntent(account, URL_102)).toBe("automatic");
    stackDiffIntentMemory.write(account, URL_102.toLowerCase(), "all");
    expect(storedStackDiffIntent(account, URL_102)).toBe("all");
    const github = stackDiffAccount({ ...TARGET, host: "github.com" });
    expect(storedStackDiffIntent(github, URL_102)).toBe("automatic");
    expect(storedStackDiffIntent(null, URL_102)).toBe("automatic");
  });
});
