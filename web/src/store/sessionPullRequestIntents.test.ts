import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pullRequestKey } from "@/lib/pullRequestReference";

import {
  beginPullRequestIntent,
  failPullRequestIntent,
  finishPullRequestIntentRequest,
  hasPullRequestIntents,
  LINK_UNCONFIRMED_REASON,
  LINK_UNREADABLE_REASON,
  PULL_REQUEST_INTENT_LIFETIME_MS,
  pullRequestChangeIsUnclear,
  pullRequestIntentEnd,
  resetPullRequestIntentsForTests,
  settlePullRequestIntents,
  UNLINK_UNCONFIRMED_REASON,
  UNLINK_UNREADABLE_REASON,
  useSessionPullRequestIntentStore,
} from "./sessionPullRequestIntents";

const SESSION = "conv_1";
const url = (number: number) => `https://github.com/acme/app/pull/${number}`;
const keys = (...numbers: number[]) =>
  new Set(numbers.map((number) => pullRequestKey(url(number))));
const intents = () => useSessionPullRequestIntentStore.getState().bySession[SESSION] ?? {};
const unconfirmed = () => useSessionPullRequestIntentStore.getState().unconfirmed[SESSION] ?? {};

beforeEach(() => resetPullRequestIntentsForTests());
afterEach(() => vi.useRealTimers());

describe("session pull request intents", () => {
  it("clears a link once a read of the list shows it, and says it settled", async () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    const end = pullRequestIntentEnd(SESSION, id);
    expect(settlePullRequestIntents(SESSION, keys(1, 2))).toBe(false);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    await expect(end).resolves.toEqual({ status: "settled" });
  });

  it("keeps a link a read disagrees with while its request still runs", () => {
    beginPullRequestIntent(SESSION, url(2), "link");
    expect(settlePullRequestIntents(SESSION, keys(1))).toBe(false);
    expect(settlePullRequestIntents(SESSION, null)).toBe(false);
    expect(Object.keys(intents())).toEqual([pullRequestKey(url(2))]);
  });

  it("reads once more when the list disagrees after the request, then lets the server win", async () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    const end = pullRequestIntentEnd(SESSION, id);
    finishPullRequestIntentRequest(SESSION, id);
    expect(settlePullRequestIntents(SESSION, keys(1))).toBe(true);
    expect(hasPullRequestIntents(SESSION)).toBe(true);
    expect(settlePullRequestIntents(SESSION, keys(1))).toBe(false);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    expect(unconfirmed()[pullRequestKey(url(2))]).toBe(LINK_UNCONFIRMED_REASON);
    await expect(end).resolves.toEqual({ status: "unconfirmed", reason: LINK_UNCONFIRMED_REASON });
  });

  it("says why a link whose answer was lost never showed, in the request's own words", () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    finishPullRequestIntentRequest(SESSION, id, "No room left.");
    settlePullRequestIntents(SESSION, keys(1));
    settlePullRequestIntents(SESSION, keys(1));
    expect(unconfirmed()[pullRequestKey(url(2))]).toBe("No room left.");
  });

  it("gives up on a link the server answered once the list can't be read twice", async () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    const end = pullRequestIntentEnd(SESSION, id);
    finishPullRequestIntentRequest(SESSION, id);
    expect(settlePullRequestIntents(SESSION, null)).toBe(true);
    expect(settlePullRequestIntents(SESSION, null)).toBe(false);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    expect(unconfirmed()[pullRequestKey(url(2))]).toBe(LINK_UNREADABLE_REASON);
    await expect(end).resolves.toEqual({ status: "unconfirmed", reason: LINK_UNREADABLE_REASON });
  });

  it("drops an unlink the server never applied, saying so only to its caller", async () => {
    const id = beginPullRequestIntent(SESSION, url(2), "unlink");
    const end = pullRequestIntentEnd(SESSION, id);
    finishPullRequestIntentRequest(SESSION, id);
    settlePullRequestIntents(SESSION, keys(1, 2));
    settlePullRequestIntents(SESSION, keys(1, 2));
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    expect(unconfirmed()).toEqual({});
    await expect(end).resolves.toEqual({
      status: "unconfirmed",
      reason: UNLINK_UNCONFIRMED_REASON,
    });
  });

  it("reverts at once when the request is refused", async () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    const end = pullRequestIntentEnd(SESSION, id);
    failPullRequestIntent(SESSION, id);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    await expect(end).resolves.toEqual({ status: "refused" });
  });

  it("lets a newer intent for the same PR replace an older one, which then changes nothing", async () => {
    const older = beginPullRequestIntent(SESSION, url(2), "unlink");
    const olderEnd = pullRequestIntentEnd(SESSION, older);
    beginPullRequestIntent(SESSION, url(2), "link");
    await expect(olderEnd).resolves.toEqual({ status: "replaced" });
    failPullRequestIntent(SESSION, older);
    finishPullRequestIntentRequest(SESSION, older);
    expect(Object.values(intents())).toMatchObject([{ action: "link", requestDone: false }]);
  });

  it("forgets an unconfirmed link once it's tried again", () => {
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    finishPullRequestIntentRequest(SESSION, id);
    settlePullRequestIntents(SESSION, keys(1));
    settlePullRequestIntents(SESSION, keys(1));
    beginPullRequestIntent(SESSION, url(2), "link");
    expect(unconfirmed()).toEqual({});
  });

  it("keeps each session's intents apart", () => {
    beginPullRequestIntent(SESSION, url(2), "link");
    beginPullRequestIntent("conv_2", url(2), "link");
    settlePullRequestIntents("conv_2", keys(2));
    expect(hasPullRequestIntents(SESSION)).toBe(true);
    expect(hasPullRequestIntents("conv_2")).toBe(false);
  });
});

describe("session pull request intents, bounded in time", () => {
  it("drops a link whose request never answers, saying it couldn't confirm", async () => {
    vi.useFakeTimers();
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    const end = pullRequestIntentEnd(SESSION, id);
    vi.advanceTimersByTime(PULL_REQUEST_INTENT_LIFETIME_MS - 1);
    expect(hasPullRequestIntents(SESSION)).toBe(true);
    vi.advanceTimersByTime(1);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    expect(unconfirmed()[pullRequestKey(url(2))]).toBe(LINK_UNREADABLE_REASON);
    await expect(end).resolves.toEqual({ status: "unconfirmed", reason: LINK_UNREADABLE_REASON });
    // Its late answer changes nothing.
    finishPullRequestIntentRequest(SESSION, id);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
  });

  it("drops an unlink whose reads never answer once its time is up", async () => {
    vi.useFakeTimers();
    const id = beginPullRequestIntent(SESSION, url(2), "unlink");
    const end = pullRequestIntentEnd(SESSION, id);
    finishPullRequestIntentRequest(SESSION, id);
    vi.advanceTimersByTime(PULL_REQUEST_INTENT_LIFETIME_MS);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    await expect(end).resolves.toEqual({
      status: "unconfirmed",
      reason: UNLINK_UNREADABLE_REASON,
    });
  });

  it("stops the clock once a read settles it", () => {
    vi.useFakeTimers();
    const id = beginPullRequestIntent(SESSION, url(2), "link");
    settlePullRequestIntents(SESSION, keys(2));
    vi.advanceTimersByTime(PULL_REQUEST_INTENT_LIFETIME_MS);
    expect(unconfirmed()).toEqual({});
    expect(id).toBeGreaterThan(0);
  });
});

describe("pullRequestChangeIsUnclear", () => {
  it.each([
    [new TypeError("Failed to fetch"), true],
    [Object.assign(new Error("conflict"), { status: 409 }), true],
    [Object.assign(new Error("timeout"), { status: 408 }), true],
    [Object.assign(new Error("gateway"), { status: 502 }), true],
    [Object.assign(new Error("bad request"), { status: 400 }), false],
    [Object.assign(new Error("forbidden"), { status: 403 }), false],
  ])("%s → %s", (error, unclear) => {
    expect(pullRequestChangeIsUnclear(error)).toBe(unclear);
  });
});
