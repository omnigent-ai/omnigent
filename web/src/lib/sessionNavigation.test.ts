import { describe, expect, it } from "vitest";
import { sessionNavigationSearch } from "./sessionNavigation";

describe("sessionNavigationSearch", () => {
  it("clears a session's file and fullscreen state while preserving unrelated parameters", () => {
    expect(
      sessionNavigationSearch(
        "?file=README.md&fullscreen=1&diff=1&comment=c1&view=chat&theme=dark",
      ),
    ).toBe("?theme=dark");
  });
});
