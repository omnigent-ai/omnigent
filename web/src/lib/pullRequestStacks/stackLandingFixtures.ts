// A stack #100 ← #101 ← #102 on the default branch, for the landing tests.

import type { LandingPullRequest, LandingStack } from "./stackLanding";
import { type StackMember, type StackMembersRead, stackMemberStatuses } from "./stackMembers";
import { type StackNode, type StackParse, stackPullRequestRef } from "./stackModel";

export const url = (number: number, repository = "acme/app") =>
  `https://github.com/${repository}/pull/${number}`;

function node(number: number, depth: number): StackNode {
  return {
    depth,
    parentIndex: depth === 0 ? null : depth - 1,
    branch: `stack/${number}`,
    ref: stackPullRequestRef(url(number)),
    external: false,
    markers: { current: false, merged: false },
  };
}

export function member(number: number, overrides: Partial<StackMember> = {}): StackMember {
  return {
    url: url(number),
    number,
    title: `PR ${number}`,
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: `stack/${number}`,
    head_sha: "c".repeat(40),
    base_ref: "main",
    base_sha: "d".repeat(40),
    head_repository: "acme/app",
    base_repository: "acme/app",
    base_default_branch: "main",
    ...overrides,
  };
}

/** #100 ← #101 ← #102, read as `self` sees it, with the members the read found. */
export function landingStack({
  self = 102,
  members = [member(100), member(101), member(102)],
  read,
  trust = "verified",
  format = "example-stack",
}: {
  self?: number;
  members?: StackMember[];
  read?: StackMembersRead;
  trust?: "verified" | "tolerated";
  format?: string;
} = {}): LandingStack {
  const nodes = [node(100, 0), node(101, 1), node(102, 2)];
  const base = { format, nodes, dependency: null, selfIndex: self - 100 };
  const parse: StackParse =
    trust === "verified"
      ? { ...base, trust, span: { start: 0, end: 0 } }
      : { ...base, trust, tolerances: [] };
  const membersRead =
    read ??
    ({ kind: "loaded", answer: { viewer: "octo", members, missing: [], unread: [] } } as const);
  return { parse, members: membersRead, statuses: stackMemberStatuses(parse, membersRead) };
}

export const pr = (overrides: Partial<LandingPullRequest> = {}): LandingPullRequest => ({
  url: url(102),
  state: "OPEN",
  head_ref: "stack/102",
  base_ref: "main",
  ...overrides,
});
