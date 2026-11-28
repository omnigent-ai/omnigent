import { describe, expect, it } from "vitest";

import type {
  StackMember,
  StackMemberStatus,
  StackMemberStatuses,
} from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import { AT_STACK_BASE, type StackDestination } from "@/lib/pullRequestStacks/stackNavigation";

import {
  stackCoverage,
  stackEntryLink,
  stackEntryView,
  stackReferenceLabel,
  stackStepCommand,
} from "./pullRequestStackModel";

const SELF: StackPullRequestRef = {
  url: "https://github.com/acme/app/pull/101",
  host: "github.com",
  repository: "acme/app",
  number: 101,
};

const node = (number: number | null): StackNode => ({
  depth: 0,
  parentIndex: null,
  branch: `stack/${number ?? "local"}`,
  ref:
    number === null
      ? null
      : {
          url: `https://github.com/acme/app/pull/${number}`,
          host: "github.com",
          repository: "acme/app",
          number,
        },
  external: false,
  markers: { current: false, merged: false },
});

const member = (over: Partial<StackMember>): StackMemberStatus => ({
  kind: "member",
  member: {
    url: "https://github.com/acme/app/pull/100",
    number: 100,
    title: "Add auth backend",
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
    ...over,
  },
});

describe("stackEntryView", () => {
  it.each([
    [{}, "Open", "open", "muted"],
    [{ review_decision: "REVIEW_REQUIRED" as const }, "Review required", "open", "muted"],
    [{ review_decision: "APPROVED" as const }, "Approved", "open", "approved"],
    [{ review_decision: "CHANGES_REQUESTED" as const }, "Changes requested", "open", "changes"],
    [{ is_draft: true, review_decision: "APPROVED" as const }, "Draft", "draft", "draft"],
    [{ state: "MERGED" as const }, "Merged", "merged", "merged"],
    [{ state: "CLOSED" as const }, "Closed", "closed", "closed"],
  ])("reads a member %o as %s", (over, status, tone, statusTone) => {
    expect(stackEntryView(node(100), member(over), SELF)).toMatchObject({
      title: "Add auth backend",
      number: "#100",
      status,
      tone,
      statusTone,
    });
  });

  it.each([
    [{ kind: "missing" } as const, "Not found"],
    [{ kind: "loading" } as const, "Loading…"],
    [{ kind: "unknown" } as const, "Status unavailable"],
    [{ kind: "external" } as const, "In another repository"],
  ])("names an entry it couldn't read by its branch: %o", (status, label) => {
    expect(stackEntryView(node(100), status, SELF)).toEqual({
      title: "stack/100",
      number: "#100",
      status: label,
      tone: "muted",
      statusTone: "muted",
      state: null,
    });
  });

  it("shows a branch not pushed yet without a number", () => {
    expect(stackEntryView(node(null), { kind: "unpushed" }, SELF)).toMatchObject({
      title: "stack/local",
      number: null,
      status: "Not pushed yet",
    });
  });
});

describe("stackStepCommand", () => {
  const destination: StackDestination = {
    index: 0,
    node: node(100),
    url: "https://github.com/acme/app/pull/100",
    unavailableReason: null,
  };

  it("names the PR it opens, asks when there are several, and says why it can't", () => {
    expect(stackStepCommand("previous", { kind: "go", destination })).toEqual({
      label: "Previous in stack (#100)",
      availability: { available: true },
      target: destination.url,
    });
    expect(stackStepCommand("next", { kind: "choose", destinations: [destination] })).toEqual({
      label: "Next in stack…",
      availability: { available: true },
      target: "choose",
    });
    expect(stackStepCommand("previous", { kind: "unavailable", reason: AT_STACK_BASE })).toEqual({
      label: "Previous in stack",
      availability: { available: false, reason: AT_STACK_BASE },
      target: null,
    });
  });
});

describe("where an entry lives and opens", () => {
  const ref = (host: string, repository: string, number: number): StackPullRequestRef => ({
    url: `https://${host}/${repository}/pull/${number}`,
    host,
    repository,
    number,
  });

  it("names as much of a PR's place as differs from this PR's", () => {
    expect(stackReferenceLabel(ref("github.com", "acme/app", 7), SELF)).toBe("#7");
    expect(stackReferenceLabel(ref("github.com", "other-org/lib", 12), SELF)).toBe(
      "other-org/lib#12",
    );
    expect(stackReferenceLabel(ref("git.example.com", "acme/app", 3), SELF)).toBe(
      "git.example.com/acme/app#3",
    );
  });

  it("opens an external entry by its own link, and a branch not pushed yet nowhere", () => {
    const external = { ...node(12), external: true, ref: ref("github.com", "other-org/lib", 12) };
    expect(stackEntryLink(external)).toEqual({
      url: "https://github.com/other-org/lib/pull/12",
      external: true,
    });
    expect(stackEntryLink(node(100))).toEqual({
      url: "https://github.com/acme/app/pull/100",
      external: false,
    });
    expect(stackEntryLink(node(null))).toBeNull();
  });
});

describe("stackCoverage", () => {
  const statuses = (
    nodes: StackMemberStatus[],
    dependency: StackMemberStatus | null = null,
  ): StackMemberStatuses => {
    const all = [...nodes, ...(dependency ? [dependency] : [])];
    const counted = all.filter((status) => !["unpushed", "external"].includes(status.kind));
    const loaded = counted.filter((status) => status.kind === "member").length;
    return {
      nodes,
      dependency,
      loaded,
      total: counted.length,
      complete: loaded === counted.length,
    };
  };
  const read = member({});

  it("says nothing once every PR's state was read", () => {
    expect(stackCoverage(statuses([read, read, { kind: "unpushed" }]))).toBeNull();
  });

  it("counts what was read of every PR in this repository, the dependency included", () => {
    expect(stackCoverage(statuses([read, { kind: "missing" }], read))).toBe(
      "Status loaded for 2 of 3",
    );
    // Past the read's cap, the rest stay unknown and the stack never reads as complete.
    expect(
      stackCoverage(statuses([...Array(50).fill(read), ...Array(7).fill({ kind: "unknown" })])),
    ).toBe("Status loaded for 50 of 57");
  });

  it("tells a read still going from one that couldn't happen", () => {
    expect(stackCoverage(statuses([{ kind: "loading" }, { kind: "loading" }]))).toBe(
      "Loading statuses…",
    );
    expect(stackCoverage(statuses([{ kind: "unknown" }, { kind: "unknown" }]))).toBe(
      "Statuses unavailable",
    );
  });
});
