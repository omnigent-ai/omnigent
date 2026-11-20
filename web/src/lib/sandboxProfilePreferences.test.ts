import { afterEach, expect, it } from "vitest";
import { readLastSandboxProfile, writeLastSandboxProfile } from "./sandboxProfilePreferences";

afterEach(() => localStorage.clear());

it("remembers the last profile per provider", () => {
  expect(readLastSandboxProfile("company")).toBeNull();
  writeLastSandboxProfile("company", "small");
  writeLastSandboxProfile("other", "build");
  writeLastSandboxProfile("company", "large");
  expect(readLastSandboxProfile("company")).toBe("large");
  expect(readLastSandboxProfile("other")).toBe("build");
  expect(readLastSandboxProfile(null)).toBeNull();
});

it("ignores a malformed stored value", () => {
  localStorage.setItem("omnigent:last-sandbox-profile-by-provider", "[1,2]");
  expect(readLastSandboxProfile("company")).toBeNull();
  localStorage.setItem(
    "omnigent:last-sandbox-profile-by-provider",
    JSON.stringify({ company: 7, other: "build" }),
  );
  expect(readLastSandboxProfile("company")).toBeNull();
  expect(readLastSandboxProfile("other")).toBe("build");
});
