import { describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import type { NativeStackAnswer } from "./nativeStack";
import type { StackMember } from "./stackMembers";
import { parseStack } from "./stackFormats";
import { stackPullRequestRef } from "./stackModel";
import {
  describedStackIdentity,
  describedStackSnapshot,
  memberInAnswer,
  nativeStackForMember,
  nativeStackSnapshot,
  sameMemberRevision,
  STACK_SNAPSHOT_MAX_AGE_MS,
  snapshotDescribes,
} from "./stackSnapshot";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;
const refOf = (number: number) => stackPullRequestRef(urlOf(number))!;
const memberOf = (number: number): StackMember => ({
  url: urlOf(number),
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: `stack/${number}`,
  head_sha: `head-${number}`,
  base_ref: "main",
  base_sha: "base",
  head_repository: "acme/app",
  base_repository: "acme/app",
});
const revisionOf = (number: number) => ({
  head_sha: `head-${number}`,
  base_ref: "main",
  base_sha: "base",
});

function nativeAnswer(
  numbers: number[],
  self: number,
  { size = numbers.length, hasNextPage = false, id = "PRS_1" } = {},
): NativeStackAnswer {
  return {
    stackEntry: { position: numbers.indexOf(self) + 1 },
    stack: {
      id,
      number: 1,
      size,
      baseRefName: "main",
      entries: {
        hasNextPage,
        nodes: numbers.map((number, index) => ({
          position: index + 1,
          pullRequest: {
            url: urlOf(number),
            number,
            state: "OPEN",
            isDraft: false,
            headRefName: `stack/${number}`,
            details: memberOf(number),
          },
        })),
      },
    },
  };
}

const body = (self: number, numbers: number[]) =>
  [
    "## Stacked changes",
    ...numbers.map((number, depth) => {
      const label = number === self ? `**stack/${number}**` : `stack/${number}`;
      return `${"  ".repeat(depth)}- [${label}](${urlOf(number)})${number === self ? " ← _this PR_" : ""}`;
    }),
    "",
    "---------",
    "The change.",
  ].join("\n");
const parseAs = (self: number, numbers: number[]) =>
  parseStack(STACK_FORMATS, body(self, numbers), refOf(self))!;

describe("stack identities", () => {
  it("names a description's stack the same from every member, and another stack apart", () => {
    const fromBase = describedStackIdentity(parseAs(100, [100, 101, 102]));
    expect(describedStackIdentity(parseAs(102, [100, 101, 102]))).toBe(fromBase);
    expect(describedStackIdentity(parseAs(101, [99, 101, 102]))).not.toBe(fromBase);
    expect(describedStackIdentity(parseAs(101, [100, 101]))).not.toBe(fromBase);
  });

  it("names GitHub's stack by its own id", () => {
    const read = { viewer: "octo", answer: nativeAnswer([100, 101], 100) };
    expect(nativeStackSnapshot(read, urlOf(100), 0)?.identity).toBe("native:PRS_1");
    const other = { viewer: "octo", answer: nativeAnswer([100, 101], 100, { id: "PRS_2" }) };
    expect(nativeStackSnapshot(other, urlOf(100), 0)?.identity).toBe("native:PRS_2");
  });

  it("records no native stack from an answer that listed none", () => {
    const read = { viewer: "octo", answer: { stackEntry: { position: 2 }, stack: null } };
    expect(nativeStackSnapshot(read, urlOf(100), 0)).toBeNull();
  });
});

describe("snapshotDescribes", () => {
  const snapshot = describedStackSnapshot(parseAs(100, [100, 101]), urlOf(100), 1_000);
  const members = {
    viewer: "octo",
    members: [memberOf(100), memberOf(101)],
    missing: [],
    unread: [],
  };

  it("stands in for a member at the head and base the shared read recorded", () => {
    expect(
      snapshotDescribes(snapshot, urlOf(101), { now: 2_000, revision: revisionOf(101), members }),
    ).toBe(true);
    // Before the member's details are known, membership alone decides.
    expect(snapshotDescribes(snapshot, urlOf(101), { now: 2_000, revision: null })).toBe(true);
    expect(
      snapshotDescribes(snapshot, urlOf(101), { now: 2_000, revision: { base_ref: "x" } }),
    ).toBe(true);
  });

  it("refuses a push, a retarget, a non-member, an unrecorded member and an old record", () => {
    const at = (revision: object, url = urlOf(101), now = 2_000) =>
      snapshotDescribes(snapshot, url, {
        now,
        revision: { ...revisionOf(101), ...revision },
        members,
      });
    expect(at({ head_sha: "pushed" })).toBe(false);
    expect(at({ base_ref: "release" })).toBe(false);
    expect(at({ base_sha: "moved" })).toBe(false);
    expect(at({}, urlOf(102))).toBe(false);
    expect(at({}, urlOf(101), 1_000 + STACK_SNAPSHOT_MAX_AGE_MS + 1)).toBe(false);
    expect(
      snapshotDescribes(snapshot, urlOf(101), {
        now: 2_000,
        revision: revisionOf(101),
        members: { ...members, members: [memberOf(100)] },
      }),
    ).toBe(false);
  });
});

describe("nativeStackForMember", () => {
  it("places another member by GitHub's own positions", () => {
    const read = { viewer: "octo", answer: nativeAnswer([100, 101, 102], 100) };
    const snapshot = nativeStackSnapshot(read, urlOf(100), 0)!;
    const evidence = snapshot.evidence.kind === "native" ? snapshot.evidence : null;
    const forMember = nativeStackForMember(evidence!, refOf(102));
    expect(forMember?.read.kind).toBe("stacked");
    expect(forMember?.answer.answer.stackEntry).toEqual({ position: 3 });
    expect(forMember?.read.kind === "stacked" && forMember.read.parse.selfIndex).toBe(2);
  });

  it("keeps a partial read's size and gaps, and gives nothing for an unread member", () => {
    const read = {
      viewer: "octo",
      answer: nativeAnswer([100, 101], 100, { size: 60, hasNextPage: true }),
    };
    const snapshot = nativeStackSnapshot(read, urlOf(100), 0)!;
    const evidence = snapshot.evidence.kind === "native" ? snapshot.evidence : null;
    const forMember = nativeStackForMember(evidence!, refOf(101));
    expect(forMember?.read).toMatchObject({ kind: "stacked-partial", size: 60, position: 2 });
    expect(forMember?.read.kind === "stacked-partial" && forMember.read.gaps).toContain(
      "more-entries",
    );
    expect(nativeStackForMember(evidence!, refOf(140))).toBeNull();
  });
});

describe("member revisions", () => {
  it("matches a member by its PR, whatever the URL's case", () => {
    const answer = { viewer: "octo", members: [memberOf(101)], missing: [], unread: [] };
    expect(memberInAnswer(answer, urlOf(101).replace("acme", "ACME"))?.number).toBe(101);
    expect(memberInAnswer(answer, urlOf(102))).toBeUndefined();
  });

  it("treats a missing value and null alike", () => {
    expect(sameMemberRevision({ head_sha: "h", base_ref: null }, { head_sha: "h" })).toBe(true);
    expect(sameMemberRevision({ head_sha: "h" }, { head_sha: "g" })).toBe(false);
  });
});
