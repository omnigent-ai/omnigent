import { afterEach, describe, expect, it, vi } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import {
  type NativeStackAnswer,
  partialNativeStack,
  readNativeStack,
} from "@/lib/pullRequestStacks/nativeStack";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";

import {
  mergeOnGitHub,
  mergeOnGitHubDescription,
  nativeStackAnswerHandoff,
  nativeStackMergeHandoff,
} from "./nativeStackMerge";
import { partialNativeStackView, pullRequestStackView } from "./usePullRequestStack";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const url = (number: number) => `https://github.com/acme/app/pull/${number}`;
const SELF = { url: url(103), host: "github.com", repository: "acme/app", number: 103 };
const SELECT = () => true;

/** GitHub's answer listing `states` at positions 1.., numbered from 101, this PR at `selfAt`. */
function answer(
  states: string[],
  selfAt: number,
  { size = states.length, hasNextPage = false } = {},
): NativeStackAnswer {
  return {
    stackEntry: { position: selfAt },
    stack: {
      id: "PRS_1",
      number: 1,
      size,
      baseRefName: "main",
      entries: {
        hasNextPage,
        nodes: states.map((state, index) => ({
          position: index + 1,
          pullRequest: {
            url: url(index + 1 === selfAt ? 103 : 100 + index + 1),
            number: index + 1 === selfAt ? 103 : 100 + index + 1,
            state,
            isDraft: false,
            headRefName: `stack/${index}`,
          },
        })),
      },
    },
  };
}

function stackOf(input: NativeStackAnswer) {
  const read = readNativeStack(input, SELF);
  if (read.kind === "stacked") {
    return pullRequestStackView(read.parse, { kind: "unavailable" }, SELECT);
  }
  if (read.kind !== "stacked-partial") throw new Error(read.kind);
  return partialNativeStackView(
    partialNativeStack(read, SELF, "stack/103"),
    SELF.url,
    { kind: "unavailable" },
    SELECT,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("nativeStackMergeHandoff", () => {
  it("counts the unmerged PRs below this one in a whole GitHub stack", () => {
    const stack = stackOf(answer(["MERGED", "OPEN", "OPEN", "OPEN"], 3));
    expect(nativeStackMergeHandoff(stack, SELF.url)).toEqual({ url: SELF.url, unmergedBelow: 1 });
    const atBase = stackOf(answer(["OPEN", "OPEN"], 1));
    expect(nativeStackMergeHandoff(atBase, SELF.url)?.unmergedBelow).toBe(0);
  });

  it("counts none below only where a partial read shows this PR at the base", () => {
    const middle = stackOf(answer(["OPEN", "OPEN", "OPEN"], 3, { size: 9, hasNextPage: true }));
    expect(nativeStackMergeHandoff(middle, SELF.url)).toEqual({
      url: SELF.url,
      unmergedBelow: null,
    });
    const base = stackOf(answer(["OPEN", "OPEN"], 1, { size: 9, hasNextPage: true }));
    expect(nativeStackMergeHandoff(base, SELF.url)?.unmergedBelow).toBe(0);
  });

  it("leaves a description's stack, and no stack, to merge here", () => {
    const body = [
      "## Stacked changes",
      `- [stack/102](${url(102)})`,
      `  - [**stack/103**](${SELF.url}) ← _this PR_`,
    ].join("\n");
    const described = parseStack(STACK_FORMATS, body, SELF);
    // A description stack, read exactly or loosely, merges here.
    expect(described?.format).not.toBe("github-native");
    expect(described).not.toBeNull();
    const view = pullRequestStackView(described!, { kind: "unavailable" }, SELECT);
    expect(nativeStackMergeHandoff(view, SELF.url)).toBeNull();
    expect(nativeStackMergeHandoff(null, SELF.url)).toBeNull();
  });
});

describe("mergeOnGitHubDescription", () => {
  it.each([
    [0, "This pull request is in a GitHub stack, so it merges on GitHub."],
    [
      1,
      "GitHub merges a stack together: merging this pull request also merges the unmerged pull request below it.",
    ],
    [
      3,
      "GitHub merges a stack together: merging this pull request also merges the 3 unmerged pull requests below it.",
    ],
    [
      null,
      "GitHub merges a stack together: merging this pull request also merges any unmerged pull requests below it.",
    ],
  ])("says what merges with %s unmerged below", (unmergedBelow, text) => {
    expect(mergeOnGitHubDescription({ url: SELF.url, unmergedBelow })).toBe(text);
  });
});

it("opens the PR on GitHub to merge it there", () => {
  const opened = vi.fn();
  vi.stubGlobal("open", opened);
  mergeOnGitHub({ url: SELF.url, unmergedBelow: 0 });
  expect(opened).toHaveBeenCalledWith(SELF.url, "_blank", "noopener,noreferrer");
});

describe("nativeStackAnswerHandoff", () => {
  it("counts the unmerged PRs below this one from a list's read of the whole stack", () => {
    expect(nativeStackAnswerHandoff(answer(["MERGED", "OPEN", "OPEN"], 3), SELF.url)).toEqual({
      url: SELF.url,
      unmergedBelow: 1,
    });
    expect(nativeStackAnswerHandoff(answer(["OPEN", "OPEN"], 1), SELF.url)?.unmergedBelow).toBe(0);
  });

  it("can't count them from a read in part, except at the base", () => {
    const partial = answer(["OPEN", "OPEN", "OPEN"], 3, { size: 9, hasNextPage: true });
    expect(nativeStackAnswerHandoff(partial, SELF.url)?.unmergedBelow).toBeNull();
    const atBase = answer(["OPEN", "OPEN"], 1, { size: 9, hasNextPage: true });
    expect(nativeStackAnswerHandoff(atBase, SELF.url)?.unmergedBelow).toBe(0);
  });

  it("leaves a PR GitHub doesn't stack, or a read that can't say, to merge here", () => {
    expect(nativeStackAnswerHandoff({ stackEntry: null, stack: null }, SELF.url)).toBeNull();
    expect(nativeStackAnswerHandoff(undefined, SELF.url)).toBeNull();
  });
});
