import { describe, expect, it } from "vitest";

import { listNativeStackFromWire } from "./nativeStackWire";

const member = (number: number, over: Record<string, unknown> = {}) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  state: "OPEN",
  is_draft: false,
  head_ref: `stack/${number}`,
  ...over,
});

const wire = (nodes: unknown[], over: Record<string, unknown> = {}) => ({
  stackEntry: { position: 2 },
  stack: {
    id: "PRS_1",
    number: 3,
    size: 3,
    baseRefName: "main",
    entries: { hasNextPage: false, nodes },
    ...over,
  },
});

describe("listNativeStackFromWire", () => {
  it("reads the list's stack, members by the few fields it names", () => {
    const answer = listNativeStackFromWire(
      wire([
        { position: 1, pullRequest: member(1) },
        { position: 2, pullRequest: member(2, { is_draft: true, state: "MERGED" }) },
      ]),
    );
    expect(answer?.stackEntry).toEqual({ position: 2 });
    expect(answer?.stack?.entries.nodes).toEqual([
      {
        position: 1,
        pullRequest: {
          url: "https://github.com/acme/app/pull/1",
          number: 1,
          state: "OPEN",
          isDraft: false,
          headRefName: "stack/1",
        },
      },
      {
        position: 2,
        pullRequest: {
          url: "https://github.com/acme/app/pull/2",
          number: 2,
          state: "MERGED",
          isDraft: true,
          headRefName: "stack/2",
        },
      },
    ]);
  });

  it("reads a member it can't trust as one this account can't see, and drops a placeless entry", () => {
    const answer = listNativeStackFromWire(
      wire([
        { position: 1, pullRequest: member(1, { head_ref: "" }) },
        { position: 2, pullRequest: member(2, { state: "DRAFTED" }) },
        { position: "3", pullRequest: member(3) },
      ]),
    );
    expect(answer?.stack?.entries.nodes).toEqual([
      { position: 1, pullRequest: null },
      { position: 2, pullRequest: null },
    ]);
  });

  it("keeps a position GitHub named without its stack, and says nothing for any other shape", () => {
    expect(listNativeStackFromWire({ stackEntry: { position: 2 }, stack: null })).toEqual({
      stackEntry: { position: 2 },
      stack: null,
    });
    expect(listNativeStackFromWire(undefined)).toBeUndefined();
    expect(listNativeStackFromWire({ stackEntry: { position: "2" }, stack: null })).toBeUndefined();
    expect(listNativeStackFromWire(wire([], { size: "3" }))).toBeUndefined();
    expect(listNativeStackFromWire(wire([], { entries: { nodes: [] } }))).toBeUndefined();
  });
});
