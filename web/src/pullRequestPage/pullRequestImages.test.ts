import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import type * as IdentityModule from "@/lib/identity";

const shell = vi.hoisted(() => ({
  route: "mac" as "mac" | "host",
  offers: true,
  generation: 1,
  userId: "user-1",
  listeners: new Set<() => void>(),
  read: vi.fn(),
}));

vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: () => shell.offers,
  macIdentity: () => ({ login: "octo", generation: shell.generation }),
  readRouteFor: (url: string) =>
    shell.route === "mac" ? { kind: "mac", target: { ...TARGET, url } } : { kind: "host" },
  subscribeDesktopReadPath: (listener: () => void) => {
    shell.listeners.add(listener);
    return () => shell.listeners.delete(listener);
  },
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => shell.userId,
  subscribeIdentity: (listener: () => void) => {
    shell.listeners.add(listener);
    return () => shell.listeners.delete(listener);
  },
}));
vi.mock("@/lib/desktopPullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestsModule>()),
  readOnThisMac: (...args: unknown[]) => shell.read(...args),
}));

import { DesktopPullRequestError } from "@/lib/desktopPullRequests";

import {
  forgetPullRequestImage,
  keptPullRequestImage,
  loadPullRequestImage,
  macLoadsPullRequestImages,
  PULL_REQUEST_IMAGE_LIMITS,
  pullRequestImageScope,
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
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
const asset = (index: number) => `0f1e2d3c-aaaa-bbbb-cccc-${String(index).padStart(12, "0")}`;
const src = (index: number) => `https://github.com/user-attachments/assets/${asset(index)}`;

// Answers each read with every asset it asked for, as PNG bytes.
const answerAll = async (_target: unknown, _operation: string, params: { assets: string[] }) => ({
  data: {
    images: params.assets.map((id) => ({ asset: id, content_type: "image/png", bytes: PNG })),
  },
  login: "octo",
});

let created = 0;
const revoked: string[] = [];
// The page showing TARGET; each test starts with one.
let leavePage: () => void;

beforeEach(() => {
  Object.assign(shell, { route: "mac", offers: true, generation: 1, userId: "user-1" });
  shell.read.mockReset();
  created = 0;
  revoked.length = 0;
  URL.createObjectURL = vi.fn(() => `blob:image-${(created += 1)}`);
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));
  leavePage = retainPullRequestImages(TARGET.url);
});

afterEach(() => {
  resetPullRequestImagesForTests();
});

const nextTick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

const changed = () => {
  for (const listener of shell.listeners) listener();
};

describe("pullRequestImageScope", () => {
  it("is set only on this Mac's route, for github.com, with a shell that loads images", () => {
    expect(pullRequestImageScope(TARGET)).not.toBeNull();
    expect(pullRequestImageScope({ ...TARGET, host: "ghe.example.com" })).toBeNull();
    expect(pullRequestImageScope({ ...TARGET, provider: "gitspoke" })).toBeNull();
    shell.route = "host";
    expect(pullRequestImageScope(TARGET)).toBeNull();
    expect(macLoadsPullRequestImages(TARGET)).toBe(true);
    shell.offers = false;
    expect(macLoadsPullRequestImages(TARGET)).toBe(false);
  });

  it("changes with the Mac account and the workspace user", () => {
    const before = pullRequestImageScope(TARGET);
    shell.generation = 2;
    expect(pullRequestImageScope(TARGET)).not.toBe(before);
    const account = pullRequestImageScope(TARGET);
    shell.userId = "user-2";
    expect(pullRequestImageScope(TARGET)).not.toBe(account);
  });
});

describe("loadPullRequestImage", () => {
  it("reads one tick's images in batches of 8, and only the PR's own params", async () => {
    shell.read.mockImplementation(answerAll);
    const results = await Promise.all(
      Array.from({ length: 9 }, (_, index) => loadPullRequestImage(TARGET, src(index))),
    );
    expect(results.every((result) => result.kind === "loaded")).toBe(true);
    expect(shell.read).toHaveBeenCalledTimes(2);
    expect(shell.read.mock.calls[0][1]).toBe("pull_request_images");
    expect(shell.read.mock.calls[0][2]).toEqual({
      repository: "octo/private",
      number: 370,
      assets: Array.from({ length: 8 }, (_, index) => asset(index)),
    });
    expect(shell.read.mock.calls[1][2].assets).toEqual([asset(8)]);
  });

  it("asks once for an image shown twice, and serves it from memory after", async () => {
    shell.read.mockImplementation(answerAll);
    const [first, second] = await Promise.all([
      loadPullRequestImage(TARGET, src(1)),
      loadPullRequestImage(TARGET, src(1)),
    ]);
    expect(first).toEqual(second);
    expect(await loadPullRequestImage(TARGET, src(1))).toEqual(first);
    expect(shell.read).toHaveBeenCalledTimes(1);
    expect(shell.read.mock.calls[0][2].assets).toEqual([asset(1)]);
  });

  it("keeps at most two reads in flight", async () => {
    const pending: (() => void)[] = [];
    let inFlight = 0;
    let most = 0;
    shell.read.mockImplementation(
      (...args: Parameters<typeof answerAll>) =>
        new Promise((resolve) => {
          inFlight += 1;
          most = Math.max(most, inFlight);
          pending.push(() => {
            inFlight -= 1;
            resolve(answerAll(...args));
          });
        }),
    );
    const loads = Array.from({ length: 40 }, (_, index) =>
      loadPullRequestImage(TARGET, src(index)),
    );
    const state = { settled: false };
    const all = Promise.all(loads).then(() => {
      state.settled = true;
    });
    // Answer one read per tick, so the queue drains while the bound is watched.
    while (!state.settled) {
      // eslint-disable-next-line no-await-in-loop -- each tick lets the queue start reads.
      await nextTick();
      expect(inFlight).toBeLessThanOrEqual(PULL_REQUEST_IMAGE_LIMITS.MAX_READS_IN_FLIGHT);
      pending.shift()?.();
    }
    await all;
    expect(most).toBe(PULL_REQUEST_IMAGE_LIMITS.MAX_READS_IN_FLIGHT);
    expect(shell.read).toHaveBeenCalledTimes(5);
  });

  it("passes the shell's reason on, and treats anything unknown as a failed read", async () => {
    shell.read.mockResolvedValue({
      data: {
        images: [
          { asset: asset(1), error: "expired" },
          { asset: asset(2), error: "something new" },
          { asset: asset(3), content_type: "text/html", bytes: PNG },
        ],
      },
      login: "octo",
    });
    const results = await Promise.all(
      [1, 2, 3, 4].map((index) => loadPullRequestImage(TARGET, src(index))),
    );
    expect(results).toEqual([
      { kind: "failed", reason: "expired" },
      { kind: "failed", reason: "query_failed" },
      { kind: "failed", reason: "query_failed" },
      { kind: "failed", reason: "query_failed" },
    ]);
    expect(created).toBe(0);
  });

  it("fails every image when the shell refuses the read", async () => {
    shell.read.mockRejectedValue(
      new DesktopPullRequestError({ code: "account_changed", message: "changed" }),
    );
    expect(await loadPullRequestImage(TARGET, src(1))).toEqual({
      kind: "failed",
      reason: "query_failed",
    });
  });

  it("refuses an image that isn't a GitHub attachment, or when off this Mac's route", async () => {
    expect((await loadPullRequestImage(TARGET, "https://example.com/a.png")).kind).toBe("failed");
    shell.route = "host";
    expect((await loadPullRequestImage(TARGET, src(1))).kind).toBe("failed");
    expect(shell.read).not.toHaveBeenCalled();
  });

  for (const [label, change] of [
    ["the Mac account changes", () => (shell.generation = 2)],
    ["the workspace user changes", () => (shell.userId = "user-2")],
    ["the PR moves to the host", () => (shell.route = "host")],
  ] as const) {
    it(`drops an answer that arrives after ${label}`, async () => {
      let answer!: () => void;
      shell.read.mockImplementation(
        (...args: Parameters<typeof answerAll>) =>
          new Promise((resolve) => {
            answer = () => resolve(answerAll(...args));
          }),
      );
      const load = loadPullRequestImage(TARGET, src(1));
      await nextTick();
      change();
      answer();
      expect(await load).toEqual({ kind: "failed", reason: "query_failed" });
      expect(created).toBe(0);
    });
  }

  it("revokes copies whose scope is no longer current, and when the page leaves", async () => {
    shell.read.mockImplementation(answerAll);
    const first = await loadPullRequestImage(TARGET, src(1));
    shell.generation = 2;
    changed();
    expect(revoked).toEqual([(first as { url: string }).url]);
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
    const second = await loadPullRequestImage(TARGET, src(1));
    leavePage();
    expect(revoked).toContain((second as { url: string }).url);
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
  });

  // A read held open until `answer`, for leaving while it runs.
  function heldRead() {
    let answer!: () => void;
    shell.read.mockImplementation(
      (...args: Parameters<typeof answerAll>) =>
        new Promise((resolve) => {
          answer = () => resolve(answerAll(...args));
        }),
    );
    return () => answer();
  }

  it("never keeps an answer that arrives after the page left", async () => {
    const answer = heldRead();
    const load = loadPullRequestImage(TARGET, src(1));
    await nextTick();
    leavePage();
    answer();
    expect(await load).toEqual({ kind: "failed", reason: "query_failed" });
    expect(created).toBe(0);
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
  });

  it("never keeps an old page's answer once a new page shows the PR", async () => {
    // Leaving and coming back, as switching PRs and back or a flag flip does.
    const answer = heldRead();
    const load = loadPullRequestImage(TARGET, src(1));
    await nextTick();
    leavePage();
    leavePage = retainPullRequestImages(TARGET.url);
    answer();
    expect((await load).kind).toBe("failed");
    expect(created).toBe(0);
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
  });

  it("leaving one PR's page keeps another PR's reads", async () => {
    const other = { ...TARGET, number: 371, url: "https://github.com/octo/private/pull/371" };
    const leaveOther = retainPullRequestImages(other.url);
    const answer = heldRead();
    const load = loadPullRequestImage(other, src(1));
    await nextTick();
    leavePage();
    answer();
    expect((await load).kind).toBe("loaded");
    leaveOther();
    expect(revoked).toHaveLength(1);
  });

  it("keeps a PR's copies while another page still shows it", async () => {
    shell.read.mockImplementation(answerAll);
    const leaveSecond = retainPullRequestImages(TARGET.url);
    const loaded = await loadPullRequestImage(TARGET, src(1));
    leavePage();
    // Releasing twice is one release.
    leavePage();
    expect(revoked).toEqual([]);
    expect(keptPullRequestImage(TARGET, src(1))).toBe((loaded as { url: string }).url);
    leaveSecond();
    expect(revoked).toEqual([(loaded as { url: string }).url]);
  });

  it("reads an image again after its copy is forgotten", async () => {
    shell.read.mockImplementation(answerAll);
    const first = await loadPullRequestImage(TARGET, src(1));
    forgetPullRequestImage(TARGET, src(1));
    expect(revoked).toEqual([(first as { url: string }).url]);
    const second = await loadPullRequestImage(TARGET, src(1));
    expect(second).not.toEqual(first);
    expect(shell.read).toHaveBeenCalledTimes(2);
  });

  it("loads nothing for a PR no page shows", async () => {
    leavePage();
    expect((await loadPullRequestImage(TARGET, src(1))).kind).toBe("failed");
    expect(shell.read).not.toHaveBeenCalled();
  });

  it("keeps at most 50 images, revoking the least recently shown", async () => {
    shell.read.mockImplementation(answerAll);
    const first = await loadPullRequestImage(TARGET, src(0));
    for (let index = 1; index <= PULL_REQUEST_IMAGE_LIMITS.MAX_KEPT_IMAGES; index += 1) {
      // Showing the first again keeps it the most recent.
      keptPullRequestImage(TARGET, src(0));
      // eslint-disable-next-line no-await-in-loop -- order is recency, so loads go one by one.
      await loadPullRequestImage(TARGET, src(index));
    }
    expect(revoked).toHaveLength(1);
    expect(revoked).not.toContain((first as { url: string }).url);
    expect(keptPullRequestImage(TARGET, src(0))).not.toBeNull();
    expect(keptPullRequestImage(TARGET, src(1))).toBeNull();
  });

  it("keeps at most 64 MiB of images", async () => {
    const big = new Uint8Array(20 * 1024 * 1024);
    shell.read.mockImplementation(
      async (_target: unknown, _operation: string, params: { assets: string[] }) => ({
        data: {
          images: params.assets.map((id) => ({ asset: id, content_type: "image/png", bytes: big })),
        },
        login: "octo",
      }),
    );
    for (let index = 0; index < 4; index += 1) {
      // eslint-disable-next-line no-await-in-loop -- order is recency, so loads go one by one.
      await loadPullRequestImage(TARGET, src(index));
    }
    expect(revoked).toEqual(["blob:image-1"]);
  });
});
