import { describe, expect, it } from "vitest";

import { recordedWorktreeRoot, worktreeRootFingerprint } from "./worktreeRoot";

// Digests produced by the server's `worktree_root_fingerprint`.
const POSIX_ROOT_FINGERPRINT = "1a1329852b078cc01f5b390143afdd4bffbbaf52772d4b7c36ff8a435bfcf287";
const WINDOWS_ROOT_FINGERPRINT = "80ecddbd344246011666ff6a111ee0be2278a0b1ca388463f48dda5983de1a61";

describe("worktreeRootFingerprint", () => {
  it("matches the server digest for POSIX and Windows roots", async () => {
    expect(await worktreeRootFingerprint("/repo/.worktrees/a/")).toBe(POSIX_ROOT_FINGERPRINT);
    expect(await worktreeRootFingerprint("c:/repo/.worktrees/a")).toBe(WINDOWS_ROOT_FINGERPRINT);
  });
});

describe("recordedWorktreeRoot", () => {
  it.each([
    ["/repo/.worktrees/a/packages/app", POSIX_ROOT_FINGERPRINT, "/repo/.worktrees/a"],
    ["/repo/.worktrees/a", POSIX_ROOT_FINGERPRINT, "/repo/.worktrees/a"],
    ["C:\\Repo\\.worktrees\\A\\pkg", WINDOWS_ROOT_FINGERPRINT, "C:\\Repo\\.worktrees\\A"],
    ["/repo/packages/app", undefined, "/repo/packages/app"],
    ["/repo/other", POSIX_ROOT_FINGERPRINT, null],
  ])("resolves %s (fingerprint %s) to %s", async (workspace, fingerprint, root) => {
    expect(await recordedWorktreeRoot(workspace, fingerprint)).toBe(root);
  });
});
