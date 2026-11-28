import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import { PullRequestApiError } from "@/lib/pullRequestRpc";

// The session's host, streaming each image as metadata and then its bytes in parts.
const host = vi.hoisted(() => ({
  reads: [] as { session: string; prUrl: string; imageUrl: string }[],
  answer: (_imageUrl: string): unknown => [
    { metadata: { contentType: "image/png", size: 4 }, data: new Uint8Array([1, 2]) },
    { data: new Uint8Array([3, 4]) },
  ],
  pending: null as Promise<void> | null,
  user: "user-1",
  identityListeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => host.user,
  subscribeIdentity: (listener: () => void) => {
    host.identityListeners.add(listener);
    return () => host.identityListeners.delete(listener);
  },
}));
vi.mock("@/lib/workspaceApi", () => ({
  callPullRequestMethod: async (
    session: string,
    method: string,
    call: (client: unknown) => Promise<unknown>,
  ) => {
    expect(method).toBe("readPullRequestImage");
    return call({
      readPullRequestImage: async function* (request: { prUrl: string; imageUrl: string }) {
        host.reads.push({ session, ...request });
        if (host.pending) await host.pending;
        const answer = host.answer(request.imageUrl);
        if (answer instanceof Error) throw answer;
        yield* answer as unknown[];
      },
    });
  },
}));

const {
  forgetPullRequestImage,
  loadPullRequestImage,
  pullRequestImageScope,
  resetPullRequestImagesForTests,
  retainPullRequestImages,
} = await import("./pullRequestImages");

const PR = "https://github.com/octo/private/pull/370";
const TARGET = { conversationId: "conv_1", prUrl: PR };
const asset = (index: number) =>
  `https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-${index.toString().padStart(12, "0")}`;

let urls = 0;
const revoked: string[] = [];

beforeEach(() => {
  host.reads = [];
  host.pending = null;
  host.user = "user-1";
  revoked.length = 0;
  vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:image-${++urls}`);
  vi.spyOn(URL, "revokeObjectURL").mockImplementation((url) => void revoked.push(url));
});
afterEach(() => {
  resetPullRequestImagesForTests();
  vi.restoreAllMocks();
  host.answer = () => [
    { metadata: { contentType: "image/png", size: 4 }, data: new Uint8Array([1, 2]) },
    { data: new Uint8Array([3, 4]) },
  ];
});

describe("pullRequestImageScope", () => {
  it("is set only for a github.com PR, and changes with the session and the workspace user", () => {
    const scope = pullRequestImageScope(TARGET);
    expect(scope).not.toBeNull();
    expect(pullRequestImageScope(null)).toBeNull();
    expect(
      pullRequestImageScope({ ...TARGET, prUrl: "https://git.example.com/octo/app/pull/1" }),
    ).toBeNull();
    expect(pullRequestImageScope({ ...TARGET, conversationId: "conv_2" })).not.toBe(scope);
    host.user = "user-2";
    expect(pullRequestImageScope(TARGET)).not.toBe(scope);
  });
});

describe("loadPullRequestImage", () => {
  it("reads the image through the session's host, joining its streamed parts", async () => {
    retainPullRequestImages(PR);
    const created = vi.mocked(URL.createObjectURL);
    const result = await loadPullRequestImage(TARGET, asset(1));
    expect(result).toEqual({ kind: "loaded", url: expect.stringMatching(/^blob:image-/) });
    expect(host.reads).toEqual([{ session: "conv_1", prUrl: PR, imageUrl: asset(1) }]);
    const blob = created.mock.calls[0]![0] as Blob;
    expect(blob.type).toBe("image/png");
    expect([...new Uint8Array(await blob.arrayBuffer())]).toEqual([1, 2, 3, 4]);
  });

  it("asks once for an image shown twice, and serves it from memory after", async () => {
    retainPullRequestImages(PR);
    const first = await loadPullRequestImage(TARGET, asset(1));
    const second = await loadPullRequestImage(TARGET, asset(1));
    expect(second).toEqual(first);
    expect(host.reads).toHaveLength(1);
  });

  it("says an image the host can't find isn't private, and refuses one that isn't an image", async () => {
    retainPullRequestImages(PR);
    host.answer = () => new PullRequestApiError("not found", 404);
    expect(await loadPullRequestImage(TARGET, asset(1))).toEqual({
      kind: "failed",
      reason: "not_found",
    });
    host.answer = () => [
      { metadata: { contentType: "text/html", size: 1 }, data: new Uint8Array([1]) },
    ];
    expect(await loadPullRequestImage(TARGET, asset(2))).toEqual({
      kind: "failed",
      reason: "unsupported_type",
    });
  });

  it("loads nothing for a PR no page shows, or a link that isn't an attachment", async () => {
    expect(await loadPullRequestImage(TARGET, asset(1))).toEqual({
      kind: "failed",
      reason: "query_failed",
    });
    retainPullRequestImages(PR);
    expect(await loadPullRequestImage(TARGET, "https://example.com/a.png")).toMatchObject({
      kind: "failed",
    });
    expect(host.reads).toEqual([]);
  });

  it("never keeps an answer that arrives after the page left", async () => {
    let finish!: () => void;
    host.pending = new Promise((resolve) => {
      finish = resolve;
    });
    const release = retainPullRequestImages(PR);
    const loading = loadPullRequestImage(TARGET, asset(1));
    release();
    finish();
    expect(await loading).toEqual({ kind: "failed", reason: "query_failed" });
  });

  it("revokes copies once the workspace user changes, and when the page leaves", async () => {
    const release = retainPullRequestImages(PR);
    const loaded = await loadPullRequestImage(TARGET, asset(1));
    host.user = "user-2";
    for (const listener of host.identityListeners) listener();
    expect(revoked).toEqual([(loaded as { url: string }).url]);

    const again = await loadPullRequestImage(TARGET, asset(1));
    release();
    expect(revoked).toContain((again as { url: string }).url);
  });

  it("reads an image again after its copy is forgotten", async () => {
    retainPullRequestImages(PR);
    await loadPullRequestImage(TARGET, asset(1));
    forgetPullRequestImage(TARGET, asset(1));
    await loadPullRequestImage(TARGET, asset(1));
    expect(host.reads).toHaveLength(2);
  });

  it("keeps at most 50 images, revoking the least recently shown", async () => {
    retainPullRequestImages(PR);
    const first = await loadPullRequestImage(TARGET, asset(0));
    await Promise.all(
      Array.from({ length: 50 }, (_, index) => loadPullRequestImage(TARGET, asset(index + 1))),
    );
    expect(revoked).toEqual([(first as { url: string }).url]);
  });
});
