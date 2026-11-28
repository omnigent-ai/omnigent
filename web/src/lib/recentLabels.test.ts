// The labels a user added recently in a repository: most recent first, kept with their colors so
// one found only by searching GitHub still shows after a restart, and dropped once GitHub says
// they're gone. Each workspace user and server keeps their own.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({
  user: "user-1" as string | null,
  server: "https://server-a.example.com" as string | null,
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => state.user,
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));

import {
  MAX_RECENT_LABELS,
  pruneRecentLabels,
  recentLabels,
  reconcileRecentLabels,
  recentLabelsOwner,
  recordRecentLabels,
  resetRecentLabelsForTests,
} from "./recentLabels";

const APP = { host: "github.com", repository: "acme/app" };
const label = (
  name: string,
  color: string | null = "ededed",
  description: string | null = null,
) => ({
  name,
  color,
  description,
});
const names = () => recentLabels(APP).map((recent) => recent.name);

beforeEach(() => {
  window.localStorage.clear();
  state.user = "user-1";
  state.server = "https://server-a.example.com";
});

afterEach(() => {
  resetRecentLabelsForTests();
  vi.restoreAllMocks();
});

describe("recording", () => {
  it("keeps added labels most recent first, once each, ignoring case", () => {
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    recordRecentLabels(APP, [label("automerge")], recentLabelsOwner());
    recordRecentLabels(APP, [label("BUG")], recentLabelsOwner());
    expect(names()).toEqual(["BUG", "automerge"]);
  });

  it(`keeps at most ${MAX_RECENT_LABELS}`, () => {
    for (let index = 0; index < MAX_RECENT_LABELS + 3; index++) {
      recordRecentLabels(APP, [label(`l${index}`)], recentLabelsOwner());
    }
    expect(names()).toHaveLength(MAX_RECENT_LABELS);
    expect(names()[0]).toBe(`l${MAX_RECENT_LABELS + 2}`);
  });

  it("keeps a label's color and description, so a searched-for one shows without a search", () => {
    recordRecentLabels(
      APP,
      [label("far-past-the-list", "c2e0c6", "Merge when green")],
      recentLabelsOwner(),
    );
    expect(recentLabels(APP)).toEqual([label("far-past-the-list", "c2e0c6", "Merge when green")]);
  });

  it("keeps each repository, host, workspace user and server apart", () => {
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    expect(recentLabels({ host: "github.com", repository: "acme/other" })).toEqual([]);
    expect(recentLabels({ host: "ghe.example.com", repository: "acme/app" })).toEqual([]);
    expect(recentLabels({ host: "GitHub.com", repository: "ACME/App" })).toHaveLength(1);
    state.user = "user-2";
    expect(recentLabels(APP)).toEqual([]);
    state.user = "user-1";
    state.server = "https://server-b.example.com";
    expect(recentLabels(APP)).toEqual([]);
  });

  it("drops other users' lists when pruned", () => {
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    state.user = "user-2";
    pruneRecentLabels();
    state.user = "user-1";
    expect(recentLabels(APP)).toEqual([]);
  });

  it("keeps nothing without a user or a stable server, and survives a full device", () => {
    state.user = null;
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    expect(Object.keys(window.localStorage)).toEqual([]);
    state.user = "user-1";
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    expect(() => recordRecentLabels(APP, [label("bug")], recentLabelsOwner())).not.toThrow();
    expect(recentLabels(APP)).toEqual([]);
  });

  it("ignores a damaged list", () => {
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    const [key] = Object.keys(window.localStorage);
    window.localStorage.setItem(key, JSON.stringify({ usedAt: 1, labels: [{ name: 42 }] }));
    expect(recentLabels(APP)).toEqual([]);
  });
});

describe("what GitHub says now", () => {
  beforeEach(() => {
    recordRecentLabels(APP, [label("gone")], recentLabelsOwner());
    recordRecentLabels(APP, [label("automerge", "000000")], recentLabelsOwner());
  });

  it("drops a label a complete list doesn't have, and takes the list's colors", () => {
    reconcileRecentLabels(APP, {
      kind: "complete",
      labels: [label("automerge", "c2e0c6", "Merge when green"), label("bug")],
    });
    expect(recentLabels(APP)).toEqual([label("automerge", "c2e0c6", "Merge when green")]);
  });

  it("drops a label a lookup says is gone, refreshes one it found, and keeps one it didn't ask about", () => {
    recordRecentLabels(APP, [label("untouched")], recentLabelsOwner());
    reconcileRecentLabels(APP, {
      kind: "lookup",
      found: new Map([
        ["gone", null],
        ["automerge", label("automerge", "c2e0c6")],
      ]),
    });
    expect(recentLabels(APP)).toEqual([label("untouched"), label("automerge", "c2e0c6")]);
  });
});
