import { describe, expect, it } from "vitest";
import { ownServerName } from "./serverNames";

describe("ownServerName", () => {
  const names = { "https://omni.example": "Acme" };

  it("looks a name up by the URL's origin", () => {
    expect(ownServerName(names, "https://omni.example/c/1?x=1")).toBe("Acme");
  });

  it("is null for other origins, bad URLs, and no names", () => {
    expect(ownServerName(names, "https://other.example/")).toBeNull();
    expect(ownServerName(names, "not a url")).toBeNull();
    expect(ownServerName(undefined, "https://omni.example/")).toBeNull();
    // Inherited keys never count as names.
    expect(ownServerName(names, "https://constructor")).toBeNull();
  });
});
