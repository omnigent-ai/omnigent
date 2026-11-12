import { describe, expect, it } from "vitest";

import { managedSandboxHostId } from "./managedSandboxHost";

describe("managedSandboxHostId", () => {
  it("mints a fresh 32-hex id per call", () => {
    const first = managedSandboxHostId();
    expect(first).toMatch(/^[0-9a-f]{32}$/);
    expect(managedSandboxHostId()).not.toBe(first);
  });
});
