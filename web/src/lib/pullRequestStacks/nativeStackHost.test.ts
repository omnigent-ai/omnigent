import { describe, expect, it, vi } from "vitest";

const rpc = vi.hoisted(() => ({ answer: undefined as unknown, calls: [] as unknown[][] }));
vi.mock("@/lib/pullRequestRpc", () => ({
  readPullRequestRpc: async (
    sessionId: string,
    method: string,
    read: (client: unknown) => Promise<{ payload?: unknown }>,
    signal?: AbortSignal,
  ) => {
    const asked: unknown[] = [];
    const client = {
      getPullRequestNativeStack: async (request: unknown) => {
        asked.push(request);
        return { payload: rpc.answer };
      },
    };
    const { payload } = await read(client);
    rpc.calls.push([sessionId, method, asked, signal]);
    return payload;
  },
}));

import {
  fetchNativeStackOnHost,
  nativeStackMembers,
  type NativeStackReadAnswer,
} from "./nativeStackHost";
import { memberFromWire, nativeStackFromWire } from "./nativeStackWire";

/** A stack read as another member's record gives one, with its members' details. */
function nativeStackAnswerFromWire(wire: {
  viewer: string;
  supported?: boolean;
  native_stack: unknown;
}): NativeStackReadAnswer | undefined {
  const answer = nativeStackFromWire(wire.native_stack, memberFromWire);
  return answer && { viewer: wire.viewer, answer };
}

// A member as the shell's stack members shaping gives it.
const member = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state: "OPEN" as const,
  is_draft: false,
  updated_at: null,
  review_decision: "APPROVED" as const,
  head_ref: `stack/${number}`,
  head_sha: String(number).padStart(40, "a"),
  base_ref: "main",
  base_sha: "b".repeat(40),
  head_repository: "acme/app",
  base_repository: "acme/app",
  base_default_branch: "main",
});
const WIRE = {
  viewer: "octo",
  supported: true,
  native_stack: {
    stackEntry: { position: 2 },
    stack: {
      id: "PRS_1",
      number: 7,
      size: 2,
      baseRefName: "main",
      entries: {
        hasNextPage: false,
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 2, pullRequest: member(102) },
        ],
      },
    },
  },
};

const SELF = { host: "github.com", repository: "acme/app", number: 102 };

describe("nativeStackMembers", () => {
  it("gives each visible member with its head commit, as the account the stack was read as", () => {
    const read = nativeStackAnswerFromWire(WIRE)!;
    expect(nativeStackMembers(read, SELF)).toEqual({
      viewer: "octo",
      members: [member(101), member(102)],
      missing: [],
      unread: [],
    });
    // The parent's head is there for counting this PR's own changes from it.
    expect(nativeStackMembers(read, SELF).members[0]!.head_sha).toBe(member(101).head_sha);
  });

  it("lists no member it couldn't see", () => {
    const read = nativeStackAnswerFromWire({
      ...WIRE,
      native_stack: {
        ...WIRE.native_stack,
        stack: {
          ...WIRE.native_stack.stack,
          entries: {
            hasNextPage: false,
            nodes: [
              { position: 1, pullRequest: null },
              { position: 2, pullRequest: member(102) },
            ],
          },
        },
      },
    })!;
    expect(nativeStackMembers(read, SELF).members.map((one) => one.number)).toEqual([102]);
  });

  const withNodes = (nodes: unknown[], size = nodes.length) =>
    nativeStackAnswerFromWire({
      ...WIRE,
      native_stack: {
        ...WIRE.native_stack,
        stack: { ...WIRE.native_stack.stack, size, entries: { hasNextPage: false, nodes } },
      },
    })!;

  it("never lets a rejected member with the parent's number stand in for the parent", () => {
    const foreign = {
      ...member(101),
      url: "https://github.com/other/app/pull/101",
      title: "Someone else's PR",
      state: "MERGED" as const,
      head_sha: "f".repeat(40),
      head_repository: "other/app",
    };
    const read = withNodes([
      { position: 1, pullRequest: member(101) },
      { position: 2, pullRequest: member(102) },
      { position: 3, pullRequest: foreign },
    ]);
    const { members } = nativeStackMembers(read, SELF);
    expect(members.map((one) => one.url)).toEqual([member(101).url, member(102).url]);
    const parent = members.find((one) => one.number === 101)!;
    expect(parent).toEqual(member(101));
  });

  it("keeps out a member listed twice, and one at a position that repeats", () => {
    const twice = withNodes([
      { position: 1, pullRequest: member(101) },
      { position: 2, pullRequest: member(101) },
      { position: 3, pullRequest: member(102) },
    ]);
    expect(nativeStackMembers(twice, SELF).members.map((one) => one.number)).toEqual([102]);
    const repeated = withNodes(
      [
        { position: 1, pullRequest: member(100) },
        { position: 1, pullRequest: member(101) },
        { position: 2, pullRequest: member(102) },
      ],
      2,
    );
    expect(nativeStackMembers(repeated, SELF).members.map((one) => one.number)).toEqual([102]);
  });

  it("has no members for a PR GitHub says isn't stacked", () => {
    const read = nativeStackAnswerFromWire({
      viewer: "octo",
      supported: true,
      native_stack: { stackEntry: null, stack: null },
    })!;
    expect(nativeStackMembers(read, SELF).members).toEqual([]);
  });
});

describe("fetchNativeStackOnHost", () => {
  it("reads the PR's stack through the session's host, by the PR's URL", async () => {
    rpc.answer = WIRE;
    rpc.calls = [];
    const signal = new AbortController().signal;

    const read = await fetchNativeStackOnHost("s1", "https://github.com/acme/app/pull/102", signal);

    expect(rpc.calls).toEqual([
      [
        "s1",
        "getPullRequestNativeStack",
        [{ prUrl: "https://github.com/acme/app/pull/102" }],
        signal,
      ],
    ]);
    expect(read?.viewer).toBe("octo");
    expect(read?.answer.stack?.entries.nodes.map(({ position }) => position)).toEqual([1, 2]);
  });

  it("reads a member the host blanked as outside the repository as a gap", async () => {
    const outside = { position: 1, pullRequest: null, outside_repository: true };
    rpc.answer = {
      ...WIRE,
      native_stack: {
        ...WIRE.native_stack,
        stack: {
          ...WIRE.native_stack.stack,
          entries: {
            hasNextPage: false,
            nodes: [outside, WIRE.native_stack.stack.entries.nodes[1]],
          },
        },
      },
    };

    const read = await fetchNativeStackOnHost("s1", "https://github.com/acme/app/pull/102");

    expect(read?.answer.stack?.entries.nodes[0]).toEqual({ position: 1, pullRequest: null });
  });

  it("can't say for a host without stacks, which answers with no viewer", async () => {
    rpc.answer = { object: "pull_request_native_stack", supported: false, viewer: null };

    expect(
      await fetchNativeStackOnHost("s1", "https://github.com/acme/app/pull/102"),
    ).toBeUndefined();
  });
});
