import { describe, expect, it } from "vitest";
import {
  soleStaticSandboxProvider,
  staticSandboxHostChoice,
  staticSandboxRefFromChoice,
} from "./staticSandboxes";

describe("static sandbox host choices", () => {
  it("round-trips the provider and an id that contains a slash", () => {
    const ref = { provider: "remote", sandbox_id: "a/b" };
    expect(staticSandboxRefFromChoice(staticSandboxHostChoice(ref))).toEqual(ref);
  });

  it("reads a host id or a malformed choice as no static sandbox", () => {
    expect(staticSandboxRefFromChoice("h1")).toBeNull();
    expect(staticSandboxRefFromChoice("__static_sandbox__:foo")).toBeNull();
    expect(staticSandboxRefFromChoice("__static_sandbox__:remote/")).toBeNull();
  });
});

describe("soleStaticSandboxProvider", () => {
  it("names the provider only when the server offers exactly one", () => {
    expect(soleStaticSandboxProvider(["remote"])).toBe("remote");
    expect(soleStaticSandboxProvider(["remote", "other"])).toBeNull();
    expect(soleStaticSandboxProvider([])).toBeNull();
    expect(soleStaticSandboxProvider(undefined)).toBeNull();
  });
});
