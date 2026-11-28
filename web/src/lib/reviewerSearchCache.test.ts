// The reviewer search cache keeps what host searches found per repository, for the workspace
// user and server they were found for, within small caps, and forgets it all on sign-out.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  user: "user-1" as string | null,
  status: { kind: "signedIn", userId: "user-1" } as
    { kind: "unknown" } | { kind: "signedIn"; userId: string | null } | { kind: "signedOut" },
  server: "server-a" as string | null,
  listeners: [] as (() => void)[],
}));
vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => identity.user,
  getIdentityStatus: () => identity.status,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.push(listener);
    return () => {};
  },
}));
vi.mock("@/lib/durableServerScope", () => ({ durableServerScope: () => identity.server }));

import {
  MAX_SAVED_AGE_MS,
  MAX_SAVED_CHARS,
  MAX_SAVED_PEOPLE,
  rememberReviewerSearch,
  resetReviewerSearchCacheForTests,
  savedReviewerSearch,
  startReviewerSearchCache,
} from "./reviewerSearchCache";

const person = (login: string) => ({ login, name: null });
const NOW = 1_800_000_000_000;

/** The identity probe settles on `user`, or on nobody signed in, as the app hears it. */
function signedInAs(user: string | null) {
  identity.user = user;
  identity.status = user === null ? { kind: "signedOut" } : { kind: "signedIn", userId: user };
  for (const listener of identity.listeners) listener();
}

const STORAGE_KEY = "omnigent:reviewer-search";

beforeEach(() => {
  identity.user = "user-1";
  identity.status = { kind: "signedIn", userId: "user-1" };
  identity.listeners = [];
  identity.server = "server-a";
  resetReviewerSearchCacheForTests();
});
afterEach(() => resetReviewerSearchCacheForTests());

describe("reviewer search cache", () => {
  it("keeps what a search found per repository, ignoring case", () => {
    rememberReviewerSearch(
      "github.com",
      "Acme/App",
      {
        people: [person("octo")],
        teams: [{ slug: "core", name: "Core" }],
      },
      NOW,
    );
    expect(savedReviewerSearch("github.com", "acme/app", NOW)).toEqual({
      people: [person("octo")],
      teams: [{ slug: "core", name: "Core" }],
    });
    expect(savedReviewerSearch("github.com", "acme/other", NOW).people).toEqual([]);
  });

  it("lists the newest search's people first, without repeats, within the cap", () => {
    const many = Array.from({ length: MAX_SAVED_PEOPLE }, (_, index) => person(`old-${index}`));
    rememberReviewerSearch("github.com", "acme/app", { people: many, teams: [] }, NOW);
    rememberReviewerSearch(
      "github.com",
      "acme/app",
      { people: [person("new"), person("OLD-0")], teams: [] },
      NOW + 1,
    );
    const { people } = savedReviewerSearch("github.com", "acme/app", NOW + 1);
    expect(people).toHaveLength(MAX_SAVED_PEOPLE);
    expect(people.slice(0, 3).map(({ login }) => login)).toEqual(["new", "OLD-0", "old-1"]);
    expect(people.some(({ login }) => login === "old-0")).toBe(false);
  });

  it("drops an entry past its age", () => {
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] }, NOW);
    expect(
      savedReviewerSearch("github.com", "acme/app", NOW + MAX_SAVED_AGE_MS + 1).people,
    ).toEqual([]);
  });

  it("never shows one user's or server's entries to another", () => {
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] }, NOW);
    identity.user = "user-2";
    expect(savedReviewerSearch("github.com", "acme/app", NOW).people).toEqual([]);
    identity.user = "user-1";
    identity.server = "server-b";
    expect(savedReviewerSearch("github.com", "acme/app", NOW).people).toEqual([]);
  });

  it("keeps nothing while signed out", () => {
    identity.user = null;
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] }, NOW);
    identity.user = "user-1";
    expect(savedReviewerSearch("github.com", "acme/app", NOW).people).toEqual([]);
  });

  it("drops the least recently found repositories past the size cap", () => {
    const big = (prefix: string) =>
      Array.from({ length: MAX_SAVED_PEOPLE }, (_, index) => ({
        login: `${prefix}-${index}`.padEnd(90, "x"),
        name: "N".repeat(200),
      }));
    // Each entry is roughly 60 KB, so five can't all fit in the cap.
    for (let index = 0; index < 5; index += 1) {
      rememberReviewerSearch(
        "github.com",
        `acme/repo-${index}`,
        {
          people: big(`r${index}`),
          teams: [],
        },
        NOW + index,
      );
    }
    const stored = window.localStorage.getItem("omnigent:reviewer-search") ?? "";
    expect(stored.length).toBeGreaterThan(0);
    expect(stored.length).toBeLessThanOrEqual(MAX_SAVED_CHARS);
    expect(savedReviewerSearch("github.com", "acme/repo-0", NOW + 5).people).toEqual([]);
    expect(savedReviewerSearch("github.com", "acme/repo-4", NOW + 5).people).toHaveLength(
      MAX_SAVED_PEOPLE,
    );
  });

  it("ignores a stored value it can't read", () => {
    window.localStorage.setItem("omnigent:reviewer-search", "{not json");
    expect(savedReviewerSearch("github.com", "acme/app", NOW).people).toEqual([]);
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] }, NOW);
    expect(savedReviewerSearch("github.com", "acme/app", NOW).people).toEqual([person("octo")]);
  });

  it("leaves nothing stored after signing out on a page that never opened a picker", () => {
    // Kept by an earlier page; this one only starts the app.
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] });
    startReviewerSearchCache();
    signedInAs(null);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("clears what was kept when the app starts signed out after a reload", () => {
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] });
    identity.user = null;
    identity.status = { kind: "signedOut" };
    startReviewerSearchCache();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("removes user A's entries from storage when user B signs in", () => {
    startReviewerSearchCache();
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] });
    signedInAs("user-2");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    signedInAs("user-1");
    expect(savedReviewerSearch("github.com", "acme/app").people).toEqual([]);
  });

  it("keeps the user's own entries while the identity probe hasn't answered", () => {
    rememberReviewerSearch("github.com", "acme/app", { people: [person("octo")], teams: [] });
    identity.status = { kind: "unknown" };
    startReviewerSearchCache();
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull();
  });
});
