import { describe, expect, it } from "vitest";

import { quoteShellArgument } from "./shell";

describe("quoteShellArgument", () => {
  it("keeps shell metacharacters inside one quoted argument", () => {
    expect(quoteShellArgument("https://example.com/api?profile=dev&glob=*")).toBe(
      "'https://example.com/api?profile=dev&glob=*'",
    );
  });

  it("escapes embedded single quotes", () => {
    expect(quoteShellArgument("don't-expand")).toBe("'don'\"'\"'t-expand'");
  });
});
