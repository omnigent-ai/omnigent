// Attachments read as the Pull Requests page's own login are kept apart from those read as the
// Mac's account for the same pull request, and go when that login signs out.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import type * as IdentityModule from "@/lib/identity";
import type * as MacNamedReadsModule from "@/lib/macNamedReads";

const shell = vi.hoisted(() => ({
  listeners: new Set<() => void>(),
  namedListeners: new Set<() => void>(),
  epochs: new Map<string, number>(),
  read: vi.fn(),
}));

vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: () => true,
  macIdentity: () => ({ login: "alice", generation: 1 }),
  readRouteFor: (url: string) => ({ kind: "mac", target: { ...TARGET, url } }),
  subscribeDesktopReadPath: (listener: () => void) => {
    shell.listeners.add(listener);
    return () => shell.listeners.delete(listener);
  },
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/macNamedReads", async (importOriginal) => ({
  ...(await importOriginal<typeof MacNamedReadsModule>()),
  namedReadsAvailable: () => true,
  namedReaderEpoch: (reader: { login: string }) => shell.epochs.get(reader.login) ?? 0,
  subscribeNamedReads: (listener: () => void) => {
    shell.namedListeners.add(listener);
    return () => shell.namedListeners.delete(listener);
  },
}));
vi.mock("@/lib/desktopPullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestsModule>()),
  readOnThisMac: (...args: unknown[]) => shell.read(...args),
}));

import {
  keptPullRequestImage,
  loadPullRequestImage,
  resetPullRequestImagesForTests,
  retainPullRequestImages,
} from "./pullRequestImages";

const TARGET: CodeHostPullRequest = {
  provider: "github",
  host: "github.com",
  repository: "octo/private",
  number: 370,
  url: "https://github.com/octo/private/pull/370",
};
const AS_BOB: CodeHostPullRequest = { ...TARGET, readAs: "bob" };
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
const asset = (index: number) => `0f1e2d3c-aaaa-bbbb-cccc-${String(index).padStart(12, "0")}`;
const src = (index: number) => `https://github.com/user-attachments/assets/${asset(index)}`;

let created = 0;
const revoked: string[] = [];

beforeEach(() => {
  shell.read.mockReset();
  shell.read.mockImplementation(
    async (target: CodeHostPullRequest, _operation: string, params: { assets: string[] }) => ({
      data: {
        images: params.assets.map((id) => ({ asset: id, content_type: "image/png", bytes: PNG })),
      },
      login: target.readAs ?? "alice",
    }),
  );
  shell.epochs.clear();
  created = 0;
  revoked.length = 0;
  URL.createObjectURL = vi.fn(() => `blob:image-${(created += 1)}`);
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));
  retainPullRequestImages(TARGET.url);
});

afterEach(() => {
  resetPullRequestImagesForTests();
});

describe("attachments read as the page's own login", () => {
  it("are kept apart from the Mac account's copies of the same pull request", async () => {
    const asBob = await loadPullRequestImage(AS_BOB, src(1));
    expect(asBob).toMatchObject({ kind: "loaded" });
    expect(shell.read.mock.calls[0]![0]).toMatchObject({ readAs: "bob" });
    // The Mac's account never sees bob's copy, and asks for its own.
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
    const asAlice = await loadPullRequestImage(TARGET, src(1));
    expect(shell.read).toHaveBeenCalledTimes(2);
    expect(shell.read.mock.calls[1]![0]).not.toHaveProperty("readAs", "bob");
    expect(asAlice).not.toEqual(asBob);
    expect(keptPullRequestImage(AS_BOB, src(1))).toEqual(
      asBob.kind === "loaded" ? asBob.url : null,
    );
  });

  it("are never read in one batch with the Mac account's", async () => {
    await Promise.all([
      loadPullRequestImage(AS_BOB, src(1)),
      loadPullRequestImage(TARGET, src(2)),
      loadPullRequestImage(AS_BOB, src(3)),
    ]);
    const batches = shell.read.mock.calls.map(([target, , params]) => ({
      readAs: (target as CodeHostPullRequest).readAs ?? null,
      assets: (params as { assets: string[] }).assets,
    }));
    expect(batches).toHaveLength(2);
    expect(batches).toContainEqual({ readAs: "bob", assets: [asset(1), asset(3)] });
    expect(batches).toContainEqual({ readAs: null, assets: [asset(2)] });
  });

  it("go when that login signs out, leaving the Mac account's", async () => {
    const asBob = await loadPullRequestImage(AS_BOB, src(1));
    const asAlice = await loadPullRequestImage(TARGET, src(1));
    shell.epochs.set("bob", 1); // bob signed out: what was read as bob must go
    for (const listener of shell.namedListeners) listener();
    expect(revoked).toEqual([asBob.kind === "loaded" ? asBob.url : ""]);
    expect(keptPullRequestImage(AS_BOB, src(1))).toBeNull();
    expect(keptPullRequestImage(TARGET, src(1))).toEqual(
      asAlice.kind === "loaded" ? asAlice.url : null,
    );
  });
});
