import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import { macOffers } from "@/lib/desktopReadPath";

import { readNativeStack } from "./nativeStack";
import {
  fetchNativeStack,
  nativeStackAnswerFromWire,
  nativeStackMembers,
  PULL_REQUEST_NATIVE_STACK_OPERATION,
} from "./nativeStackRead";

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
vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: vi.fn(),
}));

const TARGET: CodeHostPullRequest = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 102,
  url: "https://github.com/acme/app/pull/102",
};
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
const modelMember = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  state: "OPEN",
  isDraft: false,
  headRefName: `stack/${number}`,
  details: member(number),
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

beforeEach(() => {
  shell.calls = [];
  shell.answer = () => Promise.resolve({ data: WIRE, login: "octo" });
  vi.mocked(macOffers).mockReturnValue(true);
});

describe("fetchNativeStack", () => {
  it("reads GitHub's stack on this Mac into the stack model", async () => {
    const before = Date.now();
    const answer = await fetchNativeStack(TARGET);
    // Stamped with when the read went out, which its members carry on.
    expect(answer!.readAt).toBeGreaterThanOrEqual(before);
    expect(nativeStackMembers(answer!, SELF).readAt).toBe(answer!.readAt);
    expect(shell.calls).toEqual([
      // Unscheduled when called directly: a read the user asked for.
      [TARGET, PULL_REQUEST_NATIVE_STACK_OPERATION, { repository: "acme/app", number: 102 }, {}],
    ]);
    expect(answer?.viewer).toBe("octo");
    const read = readNativeStack(answer?.answer, {
      host: "github.com",
      repository: "acme/app",
      number: 102,
    });
    expect(read.kind).toBe("stacked");
  });

  it("never asks an older shell that doesn't offer the read, and can't say then", async () => {
    vi.mocked(macOffers).mockReturnValue(false);
    expect(await fetchNativeStack(TARGET)).toBeUndefined();
    expect(shell.calls).toEqual([]);
    expect(macOffers).toHaveBeenCalledWith("github", PULL_REQUEST_NATIVE_STACK_OPERATION);
  });

  it("can't say where a shell refuses the read as unsupported", async () => {
    shell.answer = () =>
      Promise.reject(new DesktopPullRequestError({ code: "unsupported", message: "No" }, TARGET));
    expect(await fetchNativeStack(TARGET)).toBeUndefined();
  });

  it("passes on any other failure, so the page can say what went wrong", async () => {
    shell.answer = () =>
      Promise.reject(
        new DesktopPullRequestError({ code: "rate_limited", message: "Wait" }, TARGET),
      );
    await expect(fetchNativeStack(TARGET)).rejects.toMatchObject({ code: "rate_limited" });
  });
});

describe("nativeStackAnswerFromWire", () => {
  it("can't say for a host without stacks, or an answer without them", () => {
    expect(nativeStackAnswerFromWire({ viewer: "octo", supported: false })).toBeUndefined();
    expect(nativeStackAnswerFromWire({ viewer: "octo", supported: true })).toBeUndefined();
    expect(nativeStackAnswerFromWire(null)).toBeUndefined();
    expect(nativeStackAnswerFromWire("stacked")).toBeUndefined();
  });

  it("keeps a confirmed absence apart from can't say, and needs the reader", () => {
    expect(
      nativeStackAnswerFromWire({
        viewer: "octo",
        supported: true,
        native_stack: { stackEntry: null, stack: null },
      }),
    ).toEqual({ viewer: "octo", answer: { stackEntry: null, stack: null } });
    expect(
      nativeStackAnswerFromWire({
        supported: true,
        native_stack: { stackEntry: null, stack: null },
      }),
    ).toBeUndefined();
  });

  it("can't say for a stack of another shape", () => {
    for (const stack of [
      { ...WIRE.native_stack.stack, id: 7 },
      { ...WIRE.native_stack.stack, size: "2" },
      { ...WIRE.native_stack.stack, entries: { nodes: [] } },
      { ...WIRE.native_stack.stack, entries: { hasNextPage: false, nodes: "none" } },
    ]) {
      expect(
        nativeStackAnswerFromWire({
          viewer: "octo",
          supported: true,
          native_stack: { stackEntry: null, stack },
        }),
      ).toBeUndefined();
    }
    expect(
      nativeStackAnswerFromWire({
        viewer: "octo",
        supported: true,
        native_stack: { stackEntry: { position: "2" }, stack: null },
      }),
    ).toBeUndefined();
  });

  it("reads a member it can't use as one this account can't see", () => {
    const answer = nativeStackAnswerFromWire({
      viewer: "octo",
      supported: true,
      native_stack: {
        ...WIRE.native_stack,
        stack: {
          ...WIRE.native_stack.stack,
          entries: {
            hasNextPage: false,
            nodes: [
              { position: 1, pullRequest: { ...member(101), is_draft: "no" } },
              { position: 2, pullRequest: member(102) },
              { position: "3", pullRequest: member(103) },
            ],
          },
        },
      },
    });
    expect(answer?.answer.stack?.entries.nodes).toEqual([
      { position: 1, pullRequest: null },
      { position: 2, pullRequest: modelMember(102) },
    ]);
  });

  it("needs each member's branch, as a member without one can't be told where it sits", () => {
    const answer = nativeStackAnswerFromWire({
      ...WIRE,
      native_stack: {
        ...WIRE.native_stack,
        stack: {
          ...WIRE.native_stack.stack,
          entries: {
            hasNextPage: false,
            nodes: [{ position: 1, pullRequest: { ...member(101), head_ref: null } }],
          },
        },
      },
    });
    expect(answer?.answer.stack?.entries.nodes).toEqual([{ position: 1, pullRequest: null }]);
  });
});

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
