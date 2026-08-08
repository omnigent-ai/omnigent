// jsdom provides localStorage; crypto.subtle comes from the node webcrypto
// shim vitest wires up. The persisted key format is part of the contract:
// changing it silently invalidates every stored grant.
import { beforeEach, describe, expect, it } from "vitest";

import {
  digestBundleBytes,
  isRepoCommandTrusted,
  isRepoDigestTrusted,
  trustRepoCommand,
  trustRepoDigest,
} from "./repoHarnessTrust";

const HOST = "host_abc";
const WS = "/tmp/demo|repo"; // deliberately contains the old delimiter

describe("repoHarnessTrust", () => {
  beforeEach(() => localStorage.clear());

  it("grants and checks a command by exact content", async () => {
    expect(await isRepoCommandTrusted(HOST, WS, "echo", "run --acp")).toBe(false);
    await trustRepoCommand(HOST, WS, "echo", "run --acp");
    expect(await isRepoCommandTrusted(HOST, WS, "echo", "run --acp")).toBe(true);
    // Any change to the command re-prompts.
    expect(await isRepoCommandTrusted(HOST, WS, "echo", "run --acp --evil")).toBe(false);
  });

  it("keeps command and bundle grants in separate namespaces", async () => {
    const digest = await digestBundleBytes(new TextEncoder().encode("bundle").buffer);
    await trustRepoDigest(HOST, WS, "echo", digest);
    expect(await isRepoDigestTrusted(HOST, WS, "echo", digest)).toBe(true);
    // A bundle grant must never satisfy a command check for the same slug.
    expect(await isRepoCommandTrusted(HOST, WS, "echo", digest)).toBe(false);
  });

  it("does not confuse delimiter-shaped path segments", async () => {
    await trustRepoCommand(HOST, "/a|b", "s", "cmd");
    expect(await isRepoCommandTrusted(HOST, "/a", "b|s", "cmd")).toBe(false);
  });
});
