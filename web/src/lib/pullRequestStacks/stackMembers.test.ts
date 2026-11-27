import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchStackMembersOnHost,
  MAX_STACK_MEMBER_LOOKUPS,
  stackMemberLookups,
  stackMemberStatuses,
  type StackMember,
  type StackMembersAnswer,
} from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

// A plain function rather than a mock, so a rejected read settles only where the code awaits it.
const host = vi.hoisted(() => ({
  calls: [] as { session: string; method: string; request: unknown }[],
  answer: (): Promise<unknown> => Promise.resolve({}),
}));

vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: async (
    session: string,
    method: string,
    call: (client: unknown) => Promise<unknown>,
  ) => {
    let request: unknown;
    await call({
      getPullRequestStack: async (asked: unknown) => {
        request = asked;
        return {};
      },
    });
    host.calls.push({ session, method, request });
    return host.answer();
  },
}));

const HOST = "git.example.com";

function node(
  number: number | null,
  options: { external?: boolean; depth?: number } = {},
): StackNode {
  return {
    depth: options.depth ?? 0,
    parentIndex: null,
    branch: `stack/${number ?? "unpushed"}`,
    ref:
      number === null
        ? null
        : {
            url: `https://${HOST}/acme/app/pull/${number}`,
            host: options.external ? "other.example.com" : HOST,
            repository: "acme/app",
            number,
          },
    external: options.external ?? false,
    markers: { current: false, merged: false },
  };
}

function stack(
  nodes: StackNode[],
  selfIndex: number,
  dependency: StackNode | null = null,
): StackParse {
  return { format: "test", trust: "tolerated", tolerances: [], nodes, dependency, selfIndex };
}

function member(number: number, overrides: Partial<StackMember> = {}): StackMember {
  return {
    url: `https://${HOST}/acme/app/pull/${number}`,
    number,
    title: `PR ${number}`,
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: `stack/${number}`,
    head_sha: null,
    base_ref: "main",
    base_sha: null,
    head_repository: "acme/app",
    base_repository: "acme/app",
    ...overrides,
  };
}

const answer = (overrides: Partial<StackMembersAnswer>): StackMembersAnswer => ({
  viewer: "octo",
  members: [],
  missing: [],
  unread: [],
  ...overrides,
});

describe("stackMemberLookups", () => {
  it("reads this PR first, then the dependency, then the rest in order, each once", () => {
    const parse = stack([node(100), node(101), node(102), node(101)], 2, node(99));
    expect(stackMemberLookups(parse)).toEqual([102, 99, 100, 101]);
  });

  it("never reads unpushed branches or PRs in other repositories", () => {
    const parse = stack([node(null), node(5, { external: true }), node(102)], 2);
    expect(stackMemberLookups(parse)).toEqual([102]);
  });

  it("stops at the cap, keeping this PR", () => {
    const nodes = Array.from({ length: 60 }, (_value, index) => node(index + 1));
    const lookups = stackMemberLookups(stack(nodes, 59));
    expect(lookups).toHaveLength(MAX_STACK_MEMBER_LOOKUPS);
    expect(lookups[0]).toBe(60);
  });
});

describe("stackMemberStatuses", () => {
  const parse = stack(
    [node(100), node(null), node(101), node(5, { external: true }), node(102)],
    4,
    node(99),
  );

  it("shows what the read found, what GitHub lacks, and leaves the rest unknown", () => {
    const statuses = stackMemberStatuses(parse, {
      kind: "loaded",
      answer: answer({ members: [member(100), member(102), member(99)], missing: [101] }),
    });
    expect(statuses.nodes.map((status) => status.kind)).toEqual([
      "member",
      "unpushed",
      "missing",
      "external",
      "member",
    ]);
    expect(statuses.dependency?.kind).toBe("member");
    // A PR GitHub lacks keeps its node, but the stack isn't complete.
    expect([statuses.loaded, statuses.total, statuses.complete]).toEqual([3, 4, false]);
  });

  it("never counts an unread PR as known", () => {
    const statuses = stackMemberStatuses(parse, {
      kind: "loaded",
      answer: answer({ members: [member(102)], unread: [100, 101, 99] }),
    });
    expect(statuses.nodes[0]?.kind).toBe("unknown");
    expect([statuses.loaded, statuses.total, statuses.complete]).toEqual([1, 4, false]);
  });

  it("shows members past the cap as unknown, so a long stack never reads as complete", () => {
    const nodes = Array.from({ length: 57 }, (_value, index) => node(index + 1));
    const long = stack(nodes, 0);
    const read = stackMemberLookups(long).map((number) => member(number));
    const statuses = stackMemberStatuses(long, {
      kind: "loaded",
      answer: answer({ members: read }),
    });
    expect([statuses.loaded, statuses.total, statuses.complete]).toEqual([50, 57, false]);
    expect(statuses.nodes.at(-1)?.kind).toBe("unknown");
  });

  it.each([
    ["unavailable", "unknown"],
    ["failed", "unknown"],
    ["loading", "loading"],
  ] as const)("marks every PR %s as %s", (kind, expected) => {
    const statuses = stackMemberStatuses(parse, { kind });
    expect(statuses.nodes.map((status) => status.kind)).toEqual([
      expected,
      "unpushed",
      expected,
      "external",
      expected,
    ]);
    expect([statuses.loaded, statuses.complete]).toEqual([0, false]);
  });

  it("is complete only when every listed PR was read", () => {
    const pair = stack([node(100), node(102)], 1);
    const read = (members: number[], missing: number[]) =>
      stackMemberStatuses(pair, {
        kind: "loaded",
        answer: answer({ members: members.map((number) => member(number)), missing }),
      });
    expect(read([], [100, 102])).toMatchObject({ loaded: 0, total: 2, complete: false });
    expect(read([102], [100])).toMatchObject({ loaded: 1, total: 2, complete: false });
    expect(read([100, 102], [])).toMatchObject({ loaded: 2, total: 2, complete: true });
  });

  it("counts a PR listed twice once", () => {
    const twice = stack([node(100), node(100), node(102)], 2);
    expect(stackMemberStatuses(twice, { kind: "unavailable" }).total).toBe(2);
  });
});

describe("fetchStackMembersOnHost", () => {
  const PR = "https://github.com/acme/app/pull/102";
  // A member as the host's branch walk lists it.
  const walked = (number: number, headRef: string, baseRef: string, current = false) => ({
    pr_url: `https://github.com/acme/app/pull/${number}`,
    number,
    title: `PR ${number}`,
    state: "OPEN",
    is_draft: false,
    head_ref: headRef,
    head_sha: String(number).padStart(40, "a"),
    base_ref: baseRef,
    current,
  });

  beforeEach(() => {
    host.calls = [];
  });

  it("reads the PR's stack through the session's host, keeping the listed PRs it found", async () => {
    host.answer = () =>
      Promise.resolve({
        object: "session.github.pr_stack",
        members: [
          walked(100, "stack/100", "main"),
          walked(103, "stack/103", "stack/102"),
          walked(102, "stack/102", "stack/100", true),
        ],
        truncated: false,
      });
    const before = Date.now();
    const result = await fetchStackMembersOnHost("s1", PR, "acme/app", [102, 100, 101]);
    expect(host.calls).toEqual([
      { session: "s1", method: "getPullRequestStack", request: { prUrl: PR } },
    ]);
    expect(result.members.map((found) => found.number)).toEqual([100, 102]);
    // A PR the walk didn't reach is unknown, never missing.
    expect(result.missing).toEqual([]);
    expect(result.unread).toEqual([101]);
    expect(result.readAt).toBeGreaterThanOrEqual(before);
    // Only a member another member stacks on names its head's repository.
    expect(result.members[0]).toMatchObject({
      head_ref: "stack/100",
      head_sha: "a".repeat(37) + "100",
      base_ref: "main",
      head_repository: "acme/app",
      base_repository: "acme/app",
    });
    expect(result.members[1]!.head_repository).toBe("acme/app");
  });

  it("reads an answer of another shape as nothing found", async () => {
    host.answer = () => Promise.resolve({ members: [{ number: "100" }, null] });
    const result = await fetchStackMembersOnHost("s1", PR, "acme/app", [102, 100]);
    expect(result.members).toEqual([]);
    expect(result.unread).toEqual([102, 100]);
  });

  it("passes on a failure", async () => {
    const error = new Error("Offline.");
    host.answer = () => Promise.reject(error);
    await expect(fetchStackMembersOnHost("s1", PR, "acme/app", [102, 100])).rejects.toBe(error);
  });
});
