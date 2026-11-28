import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const workspace = vi.hoisted(() => ({ user: "user-1" as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => workspace.user,
}));

import {
  cancelControlRequests,
  cancelControlRequestsFor,
  CONTROL_REQUEST_WAIT_MS,
  requestPullRequestControl,
  resetControlRequestsForTests,
  takeControlRequest,
  waitingControlFor,
} from "./controlRequests";

const PR = "https://github.com/acme/app/pull/42";
const OTHER = "https://github.com/acme/app/pull/43";
const PAGE = { destination: "page-detail" };

beforeEach(() => {
  workspace.user = "user-1";
  resetControlRequestsForTests();
});
afterEach(() => vi.restoreAllMocks());

describe("a request to open a pull request's control", () => {
  it("is taken once, by its own view, for its own PR and control", () => {
    requestPullRequestControl(PR, "labels", PAGE, null);
    expect(waitingControlFor("page-detail", PR)).toBe("labels");
    expect(takeControlRequest(PR, "labels", { destination: "sidebar" })).toBeUndefined();
    expect(takeControlRequest(OTHER, "labels", PAGE)).toBeUndefined();
    expect(takeControlRequest(PR, "reviewers", PAGE)).toBeUndefined();
    expect(takeControlRequest(PR, "labels", PAGE)).toBeDefined();
    expect(takeControlRequest(PR, "labels", PAGE)).toBeUndefined();
    expect(waitingControlFor("page-detail", PR)).toBeNull();
  });

  it("stops waiting after a while", () => {
    const start = Date.now();
    requestPullRequestControl(PR, "reviewers", PAGE, null);
    const late = start + CONTROL_REQUEST_WAIT_MS + 1_000;
    expect(waitingControlFor("page-detail", PR, late)).toBeNull();
    expect(takeControlRequest(PR, "reviewers", PAGE, late)).toBeUndefined();
  });

  it("is handed back only to its own view, opening on what the hand-back names", () => {
    requestPullRequestControl(PR, "review", PAGE, "APPROVE");
    takeControlRequest(PR, "review", PAGE)!.release(null);
    expect(takeControlRequest(PR, "review", { destination: "sidebar" })).toBeUndefined();
    expect(takeControlRequest(PR, "review", PAGE)?.detail).toBeNull();
  });

  it("is dropped, not left for another view, once its workspace user changes", () => {
    requestPullRequestControl(PR, "labels", PAGE, null);
    workspace.user = "user-2";
    expect(takeControlRequest(PR, "labels", PAGE)).toBeUndefined();
    workspace.user = "user-1";
    expect(takeControlRequest(PR, "labels", PAGE)).toBeUndefined();
  });

  it("is dropped as its view moves to another PR, or the view showing its PR goes", () => {
    requestPullRequestControl(PR, "labels", PAGE, null);
    cancelControlRequests("page-detail", PR);
    expect(waitingControlFor("page-detail", PR)).toBe("labels");
    cancelControlRequests("page-detail", OTHER);
    expect(waitingControlFor("page-detail", PR)).toBeNull();

    requestPullRequestControl(PR, "labels", PAGE, null);
    // The view showing another PR goes: it's replaced by one for this request's PR.
    cancelControlRequestsFor("page-detail", OTHER);
    expect(waitingControlFor("page-detail", PR)).toBe("labels");
    cancelControlRequestsFor("page-detail", PR);
    expect(waitingControlFor("page-detail", PR)).toBeNull();
  });
});
