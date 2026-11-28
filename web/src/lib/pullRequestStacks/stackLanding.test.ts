import { describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import {
  defaultBaseProven,
  type LandingPullRequest,
  type LandingStack,
  stackLandBranch,
  type StackLandingDiagnostic,
  stackLandingMessage,
  stackLandingNotice,
  stackLandingTool,
  wantsLandingDiagnostic,
} from "./stackLanding";
import { landingStack, member, pr, url } from "./stackLandingFixtures";
import type { StackMember } from "./stackMembers";

const separable: StackLandingDiagnostic = { kind: "separable" };
const TOOL = EXAMPLE_STACK_FORMAT.landing!;
const UNLANDED_FORMAT = { id: "unlanded", parse: () => null };

withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT, UNLANDED_FORMAT] });

describe("stackLandingTool", () => {
  it("is the tool of a verified stack's registered format", () => {
    expect(stackLandingTool(landingStack().parse)).toBe(TOOL);
  });

  it("is absent for a format without one, an unregistered format, or a tolerated stack", () => {
    expect(stackLandingTool(landingStack({ format: "unlanded" }).parse)).toBeNull();
    expect(stackLandingTool(landingStack({ format: "elsewhere" }).parse)).toBeNull();
    expect(stackLandingTool(landingStack({ trust: "tolerated" }).parse)).toBeNull();
  });

  it("gives no notice and no branch where no tool lands the stack", () => {
    const stack = landingStack({ format: "unlanded" });
    expect(stackLandingNotice(stack, pr(), null)).toBeNull();
    expect(stackLandBranch(stack, pr())).toBeNull();
  });
});

describe("defaultBaseProven", () => {
  it("holds when both reads agree the PR targets its repository's default branch", () => {
    expect(defaultBaseProven(landingStack(), pr())).toBe(true);
  });

  for (const [name, value] of [
    ["an older shell that doesn't name it", undefined],
    ["a repository without one", null],
    ["an empty name", ""],
    ["a name that isn't text", 7],
  ] as const) {
    it(`proves nothing for ${name}`, () => {
      const self = member(102, { base_default_branch: value as unknown as string });
      const stack = landingStack({ members: [member(100), member(101), self] });
      expect(defaultBaseProven(stack, pr())).toBe(false);
    });
  }

  it("needs the PR's base to be the default branch, exactly", () => {
    for (const base of ["release", "Main"]) {
      const self = member(102, { base_ref: base });
      const stack = landingStack({ members: [member(100), member(101), self] });
      expect(defaultBaseProven(stack, pr({ base_ref: base }))).toBe(false);
    }
  });

  it("proves nothing once the PR is retargeted, before its members are read again", () => {
    // Same head and description: the members read still names the old base.
    expect(defaultBaseProven(landingStack(), pr({ base_ref: "release" }))).toBe(false);
    expect(defaultBaseProven(landingStack(), pr({ base_ref: "stack/101" }))).toBe(false);
    expect(defaultBaseProven(landingStack(), pr({ base_ref: null }))).toBe(false);
  });

  it("needs the members read to be of the repository the page routed to", () => {
    const self = member(102, { base_repository: "other/app" });
    expect(defaultBaseProven(landingStack({ members: [self] }), pr())).toBe(false);
    const moved = member(102, { url: url(102, "other/app") });
    expect(defaultBaseProven(landingStack({ members: [moved] }), pr())).toBe(false);
    // Repository names ignore case.
    const cased = member(102, { base_repository: "Acme/App" });
    expect(defaultBaseProven(landingStack({ members: [cased] }), pr())).toBe(true);
  });

  it("proves nothing without a members read that found this PR", () => {
    for (const read of [
      { kind: "loading" },
      { kind: "failed" },
      { kind: "unavailable" },
    ] as const) {
      expect(defaultBaseProven(landingStack({ read }), pr())).toBe(false);
    }
    expect(defaultBaseProven(landingStack({ members: [member(100)] }), pr())).toBe(false);
  });
});

describe("stackLandingNotice", () => {
  const notice = (
    stack: LandingStack,
    diagnostic: StackLandingDiagnostic | null = null,
    shown = pr(),
  ) => stackLandingNotice(stack, shown, diagnostic)?.kind ?? null;
  const parentIs = (state: StackMember["state"]) =>
    landingStack({ members: [member(100), member(101, { state }), member(102)] });

  it("asks for the open parent to merge first", () => {
    expect(stackLandingNotice(landingStack(), pr(), null)).toEqual({
      kind: "merge-parent-first",
      parent: { number: 101, label: "#101" },
    });
  });

  it("says nothing of a parent that's merged and synced, merged into the base, or closed", () => {
    for (const diagnostic of [
      null,
      { kind: "not_carried" },
      { kind: "in_base" },
      { kind: "reading" },
    ] as const) {
      expect(notice(parentIs("MERGED"), diagnostic)).toBeNull();
    }
    expect(notice(parentIs("CLOSED"), separable)).toBeNull();
  });

  it("asks to sync once a squash-merged parent's commits are still in this PR", () => {
    expect(notice(parentIs("MERGED"), separable)).toBe("sync");
    // The open parent's advice comes first; there's nothing to sync yet.
    expect(notice(parentIs("OPEN"), separable)).toBe("merge-parent-first");
  });

  it("offers a way back when the check went stale or failed", () => {
    expect(notice(parentIs("MERGED"), { kind: "stale" })).toBe("stale");
    expect(notice(parentIs("MERGED"), { kind: "failed" })).toBe("failed");
  });

  it("says the parent's status is unknown only for a stack it proved", () => {
    const unread = landingStack({ members: [member(100), member(102)] });
    expect(notice(unread)).toBe("parent-unknown");
    // A members read that never came can't prove anything, so it says nothing.
    expect(notice(landingStack({ read: { kind: "loading" } }))).toBeNull();
  });

  it("says nothing for a stack it can't vouch for", () => {
    expect(notice(landingStack({ trust: "tolerated" }))).toBeNull();
    expect(notice(landingStack({ format: "other" }))).toBeNull();
    expect(notice(landingStack(), null, pr({ base_ref: "stack/101" }))).toBeNull();
  });

  it("says nothing once this PR merged or closed, though its stack's read says it's open", () => {
    // Same head and description: the members read still has this PR open.
    for (const state of ["MERGED", "CLOSED", undefined]) {
      expect(notice(parentIs("MERGED"), separable, pr({ state }))).toBeNull();
      expect(notice(parentIs("OPEN"), null, pr({ state }))).toBeNull();
      expect(wantsLandingDiagnostic(parentIs("MERGED"), pr({ state }))).toBe(false);
      expect(stackLandBranch(landingStack(), pr({ state }))).toBeNull();
    }
  });

  it("says nothing for the stack's first PR, which has nothing below it", () => {
    const root = landingStack({ self: 100 });
    expect(notice(root, null, pr({ url: url(100), head_ref: "stack/100" }))).toBeNull();
  });

  it("asks for the comparison only once the parent merged", () => {
    expect(wantsLandingDiagnostic(parentIs("MERGED"), pr())).toBe(true);
    expect(wantsLandingDiagnostic(parentIs("OPEN"), pr())).toBe(false);
    expect(wantsLandingDiagnostic(parentIs("CLOSED"), pr())).toBe(false);
    expect(wantsLandingDiagnostic(parentIs("MERGED"), pr({ base_ref: "release" }))).toBe(false);
  });

  it("puts each notice in plain words", () => {
    const parent = { number: 101, label: "#101" };
    expect(stackLandingMessage({ kind: "merge-parent-first", parent }, TOOL)).toBe(
      "Merge #101 first. A stack lands in order, starting with its first PR, and stk land does that for you.",
    );
    expect(stackLandingMessage({ kind: "sync", parent }, TOOL)).toBe(
      "#101 is merged, but this PR still includes its commits. Run stk sync && stk push before you merge.",
    );
  });
});

describe("stackLandBranch", () => {
  const branchOf = (stack: LandingStack, shown: LandingPullRequest) =>
    stackLandBranch(stack, shown);

  it("lands through the PR's head, whatever lies below it", () => {
    expect(branchOf(landingStack(), pr())).toBe("stack/102");
    const merged = landingStack({
      members: [member(100), member(101, { state: "MERGED" }), member(102)],
    });
    expect(branchOf(merged, pr())).toBe("stack/102");
    const root = landingStack({ self: 100 });
    expect(branchOf(root, pr({ url: url(100), head_ref: "stack/100" }))).toBe("stack/100");
  });

  it("lands a PR from a fork too", () => {
    const fork = member(102, { head_repository: "alice/app" });
    expect(branchOf(landingStack({ members: [fork] }), pr())).toBe("stack/102");
  });

  it("names the head GitHub read, never the description's label", () => {
    expect(branchOf(landingStack(), pr({ head_ref: "renamed" }))).toBe("renamed");
  });

  it("offers nothing for a branch name git would refuse, or none", () => {
    for (const head of ["", "-f", "a\nb", "a\u0000b", "tab\there", "del\u007f", null, undefined]) {
      expect(branchOf(landingStack(), pr({ head_ref: head }))).toBeNull();
    }
  });

  it("offers nothing for a stack it can't vouch for", () => {
    expect(branchOf(landingStack({ trust: "tolerated" }), pr())).toBeNull();
    expect(branchOf(landingStack(), pr({ base_ref: "release" }))).toBeNull();
  });
});
