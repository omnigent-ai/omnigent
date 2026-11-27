import { describe, expect, it } from "vitest";

import {
  type ListedStack,
  listedNativeStack,
  listedStackExtends,
  listedStackMembers,
} from "./listedStack";
import type { NativeStackAnswer } from "./nativeStack";
import { readNativeStack } from "./nativeStack";
import { stackPullRequestRef } from "./stackModel";
import type { StackMember } from "./stackMembers";

const url = (number: number, repository = "acme/app") =>
  `https://github.com/${repository}/pull/${number}`;

function member(number: number): StackMember {
  return {
    url: url(number),
    number,
    title: `PR ${number}`,
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: null,
    head_sha: null,
    base_ref: null,
    base_sha: null,
    head_repository: null,
    base_repository: null,
  };
}

const listing = (...numbers: number[]): ListedStack => ({
  member: (target) => {
    const found = numbers.find((number) => target === url(number));
    return found === undefined ? null : member(found);
  },
  viewer: "alice",
});

describe("listedStackMembers", () => {
  it("stands in for a members read with each member the list shows, once", () => {
    const answer = listedStackMembers(listing(101, 102), [url(101), url(102), url(101)]);
    expect(answer).toEqual({
      viewer: "alice",
      members: [member(101), member(102)],
      missing: [],
      unread: [],
    });
  });

  it("leaves a member the list doesn't show unread, never missing", () => {
    const answer = listedStackMembers(listing(102), [url(101), url(102)]);
    expect(answer.members.map((found) => found.number)).toEqual([102]);
    expect(answer.unread).toEqual([101]);
    expect(answer.missing).toEqual([]);
  });

  it("asks the list by each member's own URL, so another repository's PR isn't taken for it", () => {
    const answer = listedStackMembers(listing(101), [url(101, "acme/web")]);
    expect(answer.members).toEqual([]);
    expect(answer.unread).toEqual([101]);
  });

  it("skips a link that isn't a pull request", () => {
    const answer = listedStackMembers(listing(101), ["https://example.com/not-a-pr"]);
    expect(answer).toMatchObject({ members: [], unread: [] });
  });
});

describe("listedNativeStack", () => {
  const SIZE = 40;
  const READ_LIMIT = 20;
  const entry = (position: number) => ({
    position,
    pullRequest: {
      url: url(100 + position),
      number: 100 + position,
      state: "OPEN",
      isDraft: false,
      headRefName: `step-${position}`,
    },
  });
  /** A row's own read: the stack's first 20 entries, and its own past them, as the list reads. */
  function rowRead(position: number, stackId = "PRS_1"): NativeStackAnswer {
    const first = Array.from({ length: READ_LIMIT }, (_, index) => entry(index + 1));
    return {
      stackEntry: { position },
      stack: {
        id: stackId,
        number: 1,
        size: SIZE,
        baseRefName: "main",
        entries: {
          hasNextPage: true,
          nodes: position > READ_LIMIT ? [...first, entry(position)] : first,
        },
      },
    };
  }
  const everyRow = Array.from({ length: SIZE }, (_, index) => rowRead(index + 1));
  const readFor = (answer: NativeStackAnswer, position: number) =>
    readNativeStack(answer, stackPullRequestRef(url(100 + position))!);

  it("reads a 40-PR stack whole from the rows the list shows, though each lists 20", () => {
    expect(readFor(rowRead(11), 11).kind).toBe("stacked-partial");
    const listed = listedNativeStack(rowRead(11), everyRow);
    expect(listed.stack?.entries.hasNextPage).toBe(false);
    expect(listed.stack?.entries.nodes.map((node) => node.position)).toEqual(
      Array.from({ length: SIZE }, (_, index) => index + 1),
    );
    expect(readFor(listed, 11).kind).toBe("stacked");
  });

  it("stays partial while a member the list doesn't show leaves a position unread", () => {
    const shown = everyRow.filter((_, index) => index !== 30);
    const listed = listedNativeStack(rowRead(11), shown);
    expect(listed.stack?.entries.hasNextPage).toBe(true);
    expect(readFor(listed, 11).kind).toBe("stacked-partial");
  });

  it("takes nothing from another stack's rows", () => {
    const others = Array.from({ length: SIZE }, (_, index) => rowRead(index + 1, "PRS_2"));
    const listed = listedNativeStack(rowRead(11), others);
    expect(listed.stack?.entries.nodes).toHaveLength(READ_LIMIT);
  });

  it("leaves out a position two rows' reads disagree on", () => {
    const disagreeing: NativeStackAnswer = {
      ...rowRead(25),
      stack: {
        ...rowRead(25).stack!,
        entries: {
          hasNextPage: true,
          nodes: [{ ...entry(25), pullRequest: entry(26).pullRequest }],
        },
      },
    };
    const listed = listedNativeStack(rowRead(11), [...everyRow, disagreeing]);
    expect(listed.stack?.entries.nodes.some((node) => node.position === 25)).toBe(false);
    expect(listed.stack?.entries.hasNextPage).toBe(true);
  });

  it("keeps an answer without a stack as it is", () => {
    const none: NativeStackAnswer = { stackEntry: null, stack: null };
    expect(listedNativeStack(none, everyRow)).toBe(none);
  });
});

describe("listedStackExtends", () => {
  const entry = (position: number, number = 100 + position) => ({
    position,
    pullRequest: {
      url: url(number),
      number,
      state: "OPEN",
      isDraft: false,
      headRefName: `step-${position}`,
    },
  });
  const answer = (count: number, hasNextPage: boolean, size = 60): NativeStackAnswer => ({
    stackEntry: { position: 2 },
    stack: {
      id: "PRS_1",
      number: 1,
      size,
      baseRefName: "main",
      entries: {
        hasNextPage,
        nodes: Array.from({ length: count }, (_, index) => entry(index + 1)),
      },
    },
  });
  const whole = answer(60, false);

  it("holds where a read cut off at 50 agrees with the list's whole stack", () => {
    expect(listedStackExtends(whole, answer(50, true))).toBe(true);
  });

  it("doesn't hold for a read GitHub didn't cut off, or a list that isn't whole", () => {
    expect(listedStackExtends(whole, answer(60, false))).toBe(false);
    expect(listedStackExtends(answer(50, true), answer(50, true))).toBe(false);
  });

  it("doesn't hold where the read places another PR, or names another stack, size or place", () => {
    const capped = answer(50, true);
    const moved = {
      ...capped,
      stack: {
        ...capped.stack!,
        entries: {
          hasNextPage: true,
          nodes: [entry(1), entry(2, 999), ...capped.stack!.entries.nodes.slice(2)],
        },
      },
    };
    expect(listedStackExtends(whole, moved)).toBe(false);
    expect(listedStackExtends(whole, { ...capped, stack: { ...capped.stack!, id: "PRS_2" } })).toBe(
      false,
    );
    expect(listedStackExtends(whole, answer(50, true, 61))).toBe(false);
    expect(listedStackExtends(whole, { ...capped, stackEntry: { position: 3 } })).toBe(false);
  });
});
