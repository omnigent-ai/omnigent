import { describe, expect, it } from "vitest";

import { browserPageLabel } from "./browserPageLabel";

describe("browserPageLabel", () => {
  it.each([
    [{ url: "https://example.com/a", title: "Example docs" }, "Example docs"],
    [{ url: "https://example.com/a", title: "  Padded  " }, "Padded"],
    [{ url: "https://example.com/a", title: "https://example.com/a" }, "example.com"],
    [{ url: "https://example.com/", title: "example.com" }, "example.com"],
    [{ url: "https://example.com/", title: "" }, "example.com"],
    [{ url: "not a url", title: "Kept" }, "Kept"],
    [{ url: "", title: "about:blank" }, "Browser 2"],
    [undefined, "Browser 2"],
  ])("%o → %s", (page, expected) => {
    expect(browserPageLabel(page, "Browser 2")).toBe(expected);
  });
});
