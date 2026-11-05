import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("preserves message size alongside color and alignment in either order", () => {
    expect(cn("text-message text-center text-muted-foreground")).toBe(
      "text-message text-center text-muted-foreground",
    );
    expect(cn("text-muted-foreground text-center text-message")).toBe(
      "text-muted-foreground text-center text-message",
    );
    expect(cn("text-ui text-message")).toBe("text-message");
    expect(cn("text-message text-ui")).toBe("text-ui");
  });

  it("keeps text-ui alongside text color and alignment utilities", () => {
    expect(cn("text-ui text-center text-muted-foreground")).toBe(
      "text-ui text-center text-muted-foreground",
    );
  });

  it("treats text-ui as a font-size utility when resolving conflicts", () => {
    // The two body steps must override each other, whichever order they arrive
    // in — a component passing text-sm has to beat a default text-ui.
    expect(cn("text-sm text-ui")).toBe("text-ui");
    expect(cn("text-ui text-sm")).toBe("text-sm");
  });
});
