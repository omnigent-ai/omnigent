// The people a user requested reviews from on this device, per organization: most recent
// first, people and teams apart, and each workspace user's own.
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

import { renderHook } from "@testing-library/react";

import {
  MAX_REQUESTED_REVIEWERS,
  recordRequestedReviewers,
  requestedReviewersOwner,
  resetRequestedReviewersForTests,
  useRequestedReviewers,
} from "./requestedReviewers";

const APP = "https://github.com/acme/app/pull/1";
const WEB = "https://github.com/acme/web/pull/9";
const OTHER_ORG = "https://github.com/other/app/pull/1";
const user = (login: string) => ({ login, kind: "user" as const });

const listed = (prUrl: string) =>
  renderHook(() => useRequestedReviewers(prUrl)).result.current.map(
    ({ login, kind }) => `${kind}:${login}`,
  );

beforeEach(() => {
  window.localStorage.clear();
  state.user = "user-1";
  state.server = "https://server-a.example.com";
});

afterEach(() => {
  resetRequestedReviewersForTests();
});

describe("requested reviewers", () => {
  it("keeps the latest first, once each, across an organization's repositories", () => {
    const owner = requestedReviewersOwner();
    recordRequestedReviewers(APP, [user("ada"), user("bob")], owner);
    recordRequestedReviewers(WEB, [user("ADA"), { login: "core", kind: "team" }], owner);
    expect(listed(APP)).toEqual(["team:core", "user:ADA", "user:bob"]);
    expect(listed(OTHER_ORG)).toEqual([]);
  });

  it(`keeps at most ${MAX_REQUESTED_REVIEWERS}`, () => {
    const owner = requestedReviewersOwner();
    for (let index = 0; index < MAX_REQUESTED_REVIEWERS + 5; index++) {
      recordRequestedReviewers(APP, [user(`person-${index}`)], owner);
    }
    expect(listed(APP)).toHaveLength(MAX_REQUESTED_REVIEWERS);
    expect(listed(APP)[0]).toBe(`user:person-${MAX_REQUESTED_REVIEWERS + 4}`);
  });

  it("keeps each workspace user's own, and nothing recorded after a switch of user", () => {
    const owner = requestedReviewersOwner();
    state.user = "user-2";
    recordRequestedReviewers(APP, [user("ada")], owner);
    expect(listed(APP)).toEqual([]);
    recordRequestedReviewers(APP, [user("bob")], requestedReviewersOwner());
    state.user = "user-1";
    expect(listed(APP)).toEqual([]);
  });

  it("keeps nothing without a user or a stable server", () => {
    state.user = null;
    recordRequestedReviewers(APP, [user("ada")], requestedReviewersOwner());
    state.user = "user-1";
    expect(listed(APP)).toEqual([]);
  });
});
