import { describe, expect, it } from "vitest";

import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import type { StackFormat, StackParse } from "@/lib/pullRequestStacks/stackModel";

const SELF = { host: "git.example.com", repository: "acme/app", number: 1 };
const PARSE: StackParse = {
  format: "first",
  trust: "tolerated",
  tolerances: ["footer"],
  nodes: [],
  dependency: null,
  selfIndex: 0,
};

const format = (id: string, result: StackParse | null, seen: string[]): StackFormat => ({
  id,
  parse: () => {
    seen.push(id);
    return result;
  },
});

describe("parseStack", () => {
  it("returns the first format's stack in order and stops there", () => {
    const seen: string[] = [];
    const formats = [
      format("none", null, seen),
      format("first", PARSE, seen),
      format("later", PARSE, seen),
    ];
    expect(parseStack(formats, "body", SELF)).toBe(PARSE);
    expect(seen).toEqual(["none", "first"]);
  });

  it("finds no stack in an empty or missing description without asking any format", () => {
    const seen: string[] = [];
    const formats = [format("first", PARSE, seen)];
    expect(parseStack(formats, "", SELF)).toBeNull();
    expect(parseStack(formats, null, SELF)).toBeNull();
    expect(seen).toEqual([]);
  });
});
