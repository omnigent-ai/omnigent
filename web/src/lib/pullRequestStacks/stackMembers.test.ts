import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import {
  fetchStackMembers,
  MAX_STACK_MEMBER_LOOKUPS,
  stackMemberLookups,
  stackMemberStatuses,
  type StackMember,
  type StackMembersAnswer,
} from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

// A plain function rather than a mock, so a rejected read settles only where the code awaits it.
const shell = vi.hoisted(() => ({
  calls: [] as unknown[][],
  answer: (): Promise<unknown> => Promise.resolve({ data: {}, login: null }),
}));

vi.mock("@/lib/desktopPullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestsModule>()),
  readOnThisMac: (...args: unknown[]) => {
    shell.calls.push(args);
    return shell.answer();
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

describe("fetchStackMembers", () => {
  const target: CodeHostPullRequest = {
    provider: "github",
    host: HOST,
    repository: "acme/app",
    number: 102,
    url: `https://${HOST}/acme/app/pull/102`,
  };
  const answerWith = (data: unknown) => {
    shell.answer = () => Promise.resolve({ data, login: "octo" });
  };
  const failWith = (error: Error) => {
    shell.answer = () => Promise.reject(error);
  };

  beforeEach(() => {
    shell.calls = [];
  });

  it("reads the numbers in the PR's own repository, as the PR reads", async () => {
    answerWith(answer({ members: [member(100)], missing: [101] }));
    const result = await fetchStackMembers(target, [102, 100, 101]);
    expect(shell.calls).toEqual([
      [
        target,
        "pull_request_stack_members",
        { repository: "acme/app", numbers: [102, 100, 101] },
        {},
      ],
    ]);
    expect(result?.members.map((found) => found.number)).toEqual([100]);
    expect(result?.missing).toEqual([101]);
  });

  it("reads an answer missing its lists as nothing found, stamped with when the read went out", async () => {
    answerWith({ viewer: "octo" });
    const before = Date.now();
    const result = await fetchStackMembers(target, [102, 100]);
    expect(result).toEqual({ ...answer({}), readAt: expect.any(Number) });
    expect(result!.readAt).toBeGreaterThanOrEqual(before);
    expect(result!.readAt).toBeLessThanOrEqual(Date.now());
  });

  it("answers null when a partial shell refuses the read as unsupported", async () => {
    failWith(new DesktopPullRequestError({ code: "unsupported", message: "No." }));
    expect(await fetchStackMembers(target, [102, 100])).toBeNull();
  });

  it("passes on any other failure", async () => {
    const error = new DesktopPullRequestError({ code: "account_changed", message: "Changed." });
    failWith(error);
    await expect(fetchStackMembers(target, [102, 100])).rejects.toBe(error);
  });
});
