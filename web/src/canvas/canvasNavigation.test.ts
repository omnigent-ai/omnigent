import { describe, expect, it } from "vitest";
import { canvasSessionHref } from "./canvasNavigation";

const MIXED_SEARCH =
  "?canvas=board&session=old&file=a&file=b&diff=1&comment=c1&view=terminal&debug=1&o=123";

describe("Canvas session destinations", () => {
  it("replaces selection and removes the outgoing file/terminal state", () => {
    expect(canvasSessionHref("child", MIXED_SEARCH)).toBe(
      "/canvas?canvas=board&debug=1&o=123&session=child&view=chat",
    );
  });

  it("clears selection without dropping the board or host parameters", () => {
    expect(canvasSessionHref(null, MIXED_SEARCH)).toBe("/canvas?canvas=board&debug=1&o=123");
    expect(canvasSessionHref(null)).toBe("/canvas");
  });

  it("encodes selection as one query value and replaces duplicate session parameters", () => {
    const id = "session /?&+#";
    const url = new URL(canvasSessionHref(id, "?session=a&session=b"), "https://example.test");
    expect(url.searchParams.getAll("session")).toEqual([id]);
    expect(url.searchParams.get("view")).toBe("chat");
    expect(url.hash).toBe("");
  });
});
