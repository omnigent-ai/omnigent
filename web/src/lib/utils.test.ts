import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("keeps text-ui alongside text color and alignment utilities", () => {
    expect(cn("text-ui text-center text-muted-foreground")).toBe(
      "text-ui text-center text-muted-foreground",
    );
  });

  it("treats text-ui as a font-size utility when resolving conflicts", () => {
    expect(cn("text-sm text-ui")).toBe("text-ui");
    expect(cn("text-ui text-xs")).toBe("text-xs");
  });
});
