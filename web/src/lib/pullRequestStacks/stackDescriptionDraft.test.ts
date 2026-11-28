import { describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import { stackDescriptionSegments } from "@/lib/pullRequestStacks/stackDescription";
import {
  editableStackSuffix,
  keptStackParse,
  stackAfterSave,
  stackDraftAfterChange,
  stackDraftApplyingMine,
  stackDraftKeepsSection,
  withStackSuffix,
} from "@/lib/pullRequestStacks/stackDescriptionDraft";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const SELF = { host: "github.com", repository: "acme/app", number: 101 };
const url = (number: number) => `https://github.com/acme/app/pull/${number}`;

function section(children: number[], eol = "\n"): string {
  return [
    "## Stacked changes",
    `- [stack/100](${url(100)})`,
    `  - [**stack/101**](${url(101)}) ← _this PR_`,
    ...children.map((number) => `    - [stack/${number}](${url(number)})`),
    "",
    "---------",
    "",
  ].join(eol);
}

function segmentsOf(body: string) {
  const parse = parseStack(STACK_FORMATS, body, SELF);
  if (parse?.trust !== "verified") throw new Error("not verified");
  return stackDescriptionSegments(body, parse);
}

describe("a stack draft's body", () => {
  it("edits only the text after the stack section, keeping every byte before it", () => {
    const original = `Read this first.  \r\n\r\n${section([], "\r\n")}The change.\r\n`;
    const stack = segmentsOf(original);
    expect(stack.prefix).toBe("Read this first.  \r\n\r\n");
    expect(editableStackSuffix(original, stack)).toBe("The change.\r\n");
    const saved = withStackSuffix(stack, "The change, explained.\r\n");
    expect(saved).toBe(
      `Read this first.  \r\n\r\n${section([], "\r\n")}The change, explained.\r\n`,
    );
    expect(saved.slice(0, stack.prefix.length + stack.metadata.length)).toBe(
      original.slice(0, stack.prefix.length + stack.metadata.length),
    );
  });

  it("is based on what a save put on GitHub, for the next conflict to compare with", () => {
    const original = `${section([])}The change.\n`;
    const stack = segmentsOf(original);
    const saved = withStackSuffix(stack, "Saved.\n");
    expect(stackAfterSave(stack, saved)).toEqual({ ...stack, suffix: "Saved.\n" });
  });
});

describe("stackDraftAfterChange", () => {
  const original = `${section([])}The change.\n`;
  const stack = segmentsOf(original);
  const draft = withStackSuffix(stack, "My edit.\n");

  it("moves the edit onto a stack section a stack tool rewrote, without saving it", () => {
    const pushed = `${section([102])}The change.\n`;
    expect(stackDraftAfterChange(stack, draft, pushed, SELF)).toEqual({
      kind: "rebased",
      body: `${section([102])}My edit.\n`,
      expected: pushed,
      stack: segmentsOf(pushed),
    });
  });

  it("leaves the choice to the person when someone else changed the text too", () => {
    expect(stackDraftAfterChange(stack, draft, `${section([])}Their edit.\n`, SELF)).toEqual({
      kind: "conflict",
    });
    expect(stackDraftAfterChange(stack, draft, `${section([102])}Their edit.\n`, SELF)).toEqual({
      kind: "conflict",
    });
    expect(
      stackDraftAfterChange(stack, draft, `Above.\n${section([])}The change.\n`, SELF),
    ).toEqual({ kind: "conflict" });
  });

  it("hands the whole description back when GitHub's stack section isn't verified any more", () => {
    const loose = section([]).replace("---------", "---");
    expect(stackDraftAfterChange(stack, draft, `${loose}The change.\n`, SELF)).toEqual({
      kind: "conflict-full-body",
    });
    expect(stackDraftAfterChange(stack, draft, "Just a change.\n", SELF)).toEqual({
      kind: "conflict-full-body",
    });
  });

  it("hands the whole description back when the draft no longer starts with its section", () => {
    const rewritten = "Rewritten, section and all.\n";
    expect(stackDraftAfterChange(stack, rewritten, `${section([102])}The change.\n`, SELF)).toEqual(
      { kind: "conflict-full-body" },
    );
    expect(stackDraftKeepsSection(rewritten, stack)).toBe(false);
    expect(stackDraftKeepsSection(draft, stack)).toBe(true);
  });
});

describe("stackDraftApplyingMine", () => {
  const original = `${section([])}The change.\n`;
  const stack = segmentsOf(original);
  const draft = withStackSuffix(stack, "My edit.\n");

  it("puts the person's text after GitHub's own stack section and text above it", () => {
    const now = `Their note.\n${section([102])}Their edit.\n`;
    expect(stackDraftApplyingMine(stack, draft, now, SELF)).toEqual({
      body: `Their note.\n${section([102])}My edit.\n`,
      stack: segmentsOf(now),
    });
  });

  it("has nothing to splice onto once GitHub's stack section isn't verified", () => {
    expect(stackDraftApplyingMine(stack, draft, "Just a change.\n", SELF)).toBeNull();
  });

  it("splices nothing into a draft that no longer starts with its section", () => {
    const now = `${section([102])}Their edit.\n`;
    expect(stackDraftApplyingMine(stack, "Rewritten.\n", now, SELF)).toBeNull();
  });
});

describe("keptStackParse", () => {
  it("reads the stack the draft's own section lists", () => {
    const parse = keptStackParse(segmentsOf(`${section([102])}The change.\n`), SELF);
    expect(parse?.nodes.map((node) => node.ref?.number)).toEqual([100, 101, 102]);
  });
});
