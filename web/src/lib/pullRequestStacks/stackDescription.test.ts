import { describe, expect, it } from "vitest";

import {
  rebaseStackDescriptionEdit,
  stackDescriptionSegments,
  withEditedSuffix,
} from "@/lib/pullRequestStacks/stackDescription";
import type { VerifiedStackParse } from "@/lib/pullRequestStacks/stackModel";

const METADATA = "<stack metadata>\r\n";
const FRESH_METADATA = "<rewritten stack metadata, longer>\r\n";

function verified(body: string, metadata: string): VerifiedStackParse {
  const start = body.indexOf(metadata);
  return {
    format: "test",
    trust: "verified",
    span: { start, end: start + metadata.length },
    nodes: [],
    dependency: null,
    selfIndex: 0,
  };
}

const original = (prefix: string, suffix: string) => {
  const body = prefix + METADATA + suffix;
  return stackDescriptionSegments(body, verified(body, METADATA));
};

describe("stackDescriptionSegments", () => {
  it("splits into prefix, metadata and suffix that join back to the exact bytes", () => {
    const body = "note above\r\n" + METADATA + "## Summary\r\nText.\r\n";
    const segments = stackDescriptionSegments(body, verified(body, METADATA));
    expect(segments).toEqual({
      prefix: "note above\r\n",
      metadata: METADATA,
      suffix: "## Summary\r\nText.\r\n",
    });
    expect(segments.prefix + segments.metadata + segments.suffix).toBe(body);
  });
});

describe("withEditedSuffix", () => {
  it("keeps the original prefix and metadata whatever the edited suffix's length", () => {
    const segments = original("above\n", "old");
    expect(withEditedSuffix(segments, "")).toBe("above\n" + METADATA);
    expect(withEditedSuffix(segments, "a much longer description\n")).toBe(
      "above\n" + METADATA + "a much longer description\n",
    );
  });
});

describe("rebaseStackDescriptionEdit", () => {
  it("moves the edit onto rewritten metadata as an unsaved draft expecting the fresh body", () => {
    const segments = original("", "old text");
    const fresh = FRESH_METADATA + "old text";
    expect(
      rebaseStackDescriptionEdit(segments, "new text", fresh, verified(fresh, FRESH_METADATA)),
    ).toEqual({
      kind: "rebased",
      draft: FRESH_METADATA + "new text",
      expected: fresh,
    });
  });

  it("is a conflict when someone else edited the description text", () => {
    const segments = original("", "old text");
    const fresh = METADATA + "their text";
    expect(
      rebaseStackDescriptionEdit(segments, "new text", fresh, verified(fresh, METADATA)),
    ).toEqual({
      kind: "conflict",
    });
  });

  it("is a conflict when the metadata and the text both changed", () => {
    const segments = original("", "old text");
    const fresh = FRESH_METADATA + "their text";
    expect(
      rebaseStackDescriptionEdit(segments, "new", fresh, verified(fresh, FRESH_METADATA)).kind,
    ).toBe("conflict");
  });

  it("is a conflict when the text above the metadata changed", () => {
    const segments = original("above\n", "old text");
    const fresh = "edited above\n" + FRESH_METADATA + "old text";
    expect(
      rebaseStackDescriptionEdit(segments, "new", fresh, verified(fresh, FRESH_METADATA)).kind,
    ).toBe("conflict");
  });

  it("is a conflict when the fresh description has no verified metadata", () => {
    expect(rebaseStackDescriptionEdit(original("", "old"), "new", "plain", null).kind).toBe(
      "conflict",
    );
  });
});
