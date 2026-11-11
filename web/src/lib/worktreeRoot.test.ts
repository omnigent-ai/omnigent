import { describe, expect, it } from "vitest";

import {
  recordedWorktreeRoot,
  sha256Hex,
  worktreeDirectoryName,
  worktreeRootFingerprint,
} from "./worktreeRoot";

// Digests and roots below come from the server's `worktree_root_fingerprint`
// and `recorded_worktree_root`.
const POSIX_ROOT = "1a1329852b078cc01f5b390143afdd4bffbbaf52772d4b7c36ff8a435bfcf287";
const WINDOWS_ROOT = "80ecddbd344246011666ff6a111ee0be2278a0b1ca388463f48dda5983de1a61";
const DRIVE_ROOT = "826c0d7d3c42f5c52210bfc49965904f2b74650b86e66ce4b14a619735bc8360";
const UNC_ROOT = "fbeb73f29978309234cc3d43e60feabd9a0d71b873360974863c49c5be27d82c";

describe("sha256Hex", () => {
  it.each([
    ["", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    ["abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
    // 55 bytes pad into one block; 56 and 64 need a second.
    ["a".repeat(55), "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"],
    ["a".repeat(56), "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a"],
    ["a".repeat(64), "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"],
    [
      `${"a".repeat(56)}${"b".repeat(9)}`,
      "56bce9672bbaf089c935b799ddf6b2268cd01913d9ab09f0ae00f361ab8b2758",
    ],
    ["é/дир/世界", "06bb4f3bb1681521f35b1209e6581af630057f277390dc0f6cd2f7ccc9dd74a9"],
  ])("hashes %j", (text, digest) => {
    expect(sha256Hex(text)).toBe(digest);
  });
});

describe("worktreeRootFingerprint", () => {
  it("matches the server digest for POSIX, Windows, drive, and UNC roots", () => {
    expect(worktreeRootFingerprint("/repo/.worktrees/a/")).toBe(POSIX_ROOT);
    expect(worktreeRootFingerprint("c:/repo/.worktrees/a")).toBe(WINDOWS_ROOT);
    expect(worktreeRootFingerprint("C:\\")).toBe(DRIVE_ROOT);
    expect(worktreeRootFingerprint("\\\\srv\\share\\wt")).toBe(UNC_ROOT);
  });
});

describe("recordedWorktreeRoot", () => {
  it.each([
    ["/repo/.worktrees/a/packages/app", POSIX_ROOT, "/repo/.worktrees/a"],
    ["/repo//.worktrees/./a/", POSIX_ROOT, "/repo/.worktrees/a"],
    ["C:/Repo/.worktrees/A/pkg", WINDOWS_ROOT, "C:\\Repo\\.worktrees\\A"],
    ["c:\\repo\\.worktrees\\a", WINDOWS_ROOT, "c:\\repo\\.worktrees\\a"],
    ["C:\\Repo", DRIVE_ROOT, "C:\\"],
    ["//srv/share/wt/pkg", UNC_ROOT, "\\\\srv\\share\\wt"],
    ["/repo/packages/app", undefined, "/repo/packages/app"],
    ["/repo/other", POSIX_ROOT, null],
  ])("resolves %s (fingerprint %s) to %s", (workspace, fingerprint, root) => {
    expect(recordedWorktreeRoot(workspace, fingerprint)).toBe(root);
  });
});

describe("worktreeDirectoryName", () => {
  it.each([
    ["/repo/.worktrees/login", "login"],
    ["C:\\Repo\\wt", "wt"],
    ["\\\\srv\\share\\wt", "wt"],
    ["C:\\", "C:\\"],
    ["/", "/"],
  ])("names %s %s", (path, name) => {
    expect(worktreeDirectoryName(path)).toBe(name);
  });
});
