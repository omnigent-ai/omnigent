import { describe, expect, it } from "vitest";

import type { StackParse } from "./stackModel";
import {
  descriptionMetadataEditable,
  mergesOnGitHub,
  navigableStack,
  trustedForComparison,
} from "./stackTrust";

const base = { format: "test", nodes: [], dependency: null, selfIndex: 0 };
const PARSES: Record<StackParse["trust"], StackParse> = {
  verified: { ...base, trust: "verified", span: { start: 0, end: 0 } },
  tolerated: { ...base, trust: "tolerated", tolerances: ["indentation"] },
  native: { ...base, format: "github-native", trust: "native" },
};

describe("stack trust gates", () => {
  it("lets every kind of stack be navigated", () => {
    for (const parse of Object.values(PARSES)) expect(navigableStack(parse)).toBe(true);
  });

  it("compares against a parent only for an exact description read or GitHub's own stack", () => {
    expect(trustedForComparison(PARSES.verified)).toBe(true);
    expect(trustedForComparison(PARSES.native)).toBe(true);
    expect(trustedForComparison(PARSES.tolerated)).toBe(false);
  });

  it("edits description metadata only when it was read exactly", () => {
    expect(descriptionMetadataEditable(PARSES.verified)).toBe(true);
    expect(descriptionMetadataEditable(PARSES.tolerated)).toBe(false);
    expect(descriptionMetadataEditable(PARSES.native)).toBe(false);
  });

  it("merges only GitHub's own stack on GitHub", () => {
    expect(mergesOnGitHub(PARSES.native)).toBe(true);
    expect(mergesOnGitHub(PARSES.verified)).toBe(false);
    expect(mergesOnGitHub(PARSES.tolerated)).toBe(false);
  });
});
