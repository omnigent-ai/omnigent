import { afterEach, expect, it } from "vitest";
import { readLastSandboxProfile, writeLastSandboxProfile } from "./sandboxProfilePreferences";

afterEach(() => localStorage.clear());

it("remembers the last profile per provider", () => {
  expect(readLastSandboxProfile("arclet")).toBeNull();
  writeLastSandboxProfile("arclet", "arm");
  writeLastSandboxProfile("company", "build");
  writeLastSandboxProfile("arclet", "x86");
  expect(readLastSandboxProfile("arclet")).toBe("x86");
  expect(readLastSandboxProfile("company")).toBe("build");
  expect(readLastSandboxProfile(null)).toBeNull();
});

it("ignores a malformed stored value", () => {
  localStorage.setItem("omnigent:last-sandbox-profile-by-provider", "[1,2]");
  expect(readLastSandboxProfile("arclet")).toBeNull();
  localStorage.setItem(
    "omnigent:last-sandbox-profile-by-provider",
    JSON.stringify({ arclet: 7, company: "build" }),
  );
  expect(readLastSandboxProfile("arclet")).toBeNull();
  expect(readLastSandboxProfile("company")).toBe("build");
});
