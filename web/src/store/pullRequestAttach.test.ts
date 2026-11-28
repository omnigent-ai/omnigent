import { beforeEach, describe, expect, it } from "vitest";

import {
  attachPullRequest,
  linkPullRequestAttempt,
  pullRequestLinkState,
  usePullRequestAttachStore,
} from "./pullRequestAttach";

const store = () => usePullRequestAttachStore.getState();

function deferred() {
  let resolve!: (url: string | undefined) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<string | undefined>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  usePullRequestAttachStore.setState({
    openSessionId: null,
    drafts: {},
    attempts: {},
    lastAttached: null,
  });
});

describe("attachPullRequest", () => {
  it("allows one pending link per session, independently across sessions", async () => {
    const first = deferred();
    const calls: string[] = [];
    const attach = (url: string) => {
      calls.push(url);
      return first.promise;
    };
    const linking = attachPullRequest("a", "https://github.com/acme/app/pull/1", attach);
    await expect(
      attachPullRequest("a", "https://github.com/acme/app/pull/2", attach),
    ).resolves.toBeNull();
    void attachPullRequest("b", "https://github.com/acme/app/pull/3", () => new Promise(() => {}));
    expect(calls).toEqual(["https://github.com/acme/app/pull/1"]);
    expect(store().attempts.b?.status).toBe("pending");

    first.resolve(undefined);
    await expect(linking).resolves.toBe("https://github.com/acme/app/pull/1");
    expect(store().attempts.a).toBeUndefined();
    expect(store().attempts.b?.status).toBe("pending");
  });

  it("clears the submitted draft and closes only its own session's picker", async () => {
    store().setDraft("a", "#1");
    store().open("b");
    await attachPullRequest("a", "https://github.com/acme/app/pull/1", async () => undefined);
    expect(store()).toMatchObject({
      openSessionId: "b",
      drafts: {},
      lastAttached: { sessionId: "a", url: "https://github.com/acme/app/pull/1" },
    });

    store().open("a");
    store().setDraft("a", "#1");
    const pending = deferred();
    const linking = attachPullRequest(
      "a",
      "https://github.com/acme/app/pull/1",
      () => pending.promise,
    );
    store().setDraft("a", "#12");
    pending.resolve("https://github.com/acme/app/pull/1");
    await linking;
    expect(store().openSessionId).toBeNull();
    expect(store().drafts.a).toBe("#12");
  });

  it("keeps a failure until its query changes, then allows a retry", async () => {
    store().setDraft("a", "#1");
    await expect(
      attachPullRequest("a", "https://github.com/acme/app/pull/1", async () => {
        throw new Error("GitHub said no.");
      }),
    ).resolves.toBeNull();
    expect(store().attempts.a).toMatchObject({ status: "failed", message: "GitHub said no." });
    store().setDraft("a", "#1");
    expect(store().attempts.a?.status).toBe("failed");
    store().setDraft("a", "#12");
    expect(store().attempts.a).toBeUndefined();
  });

  it("ignores a completion for an attempt that is no longer the latest", () => {
    const stale = store().startAttempt("a")!;
    store().failAttempt("a", stale, "No access.");
    store().setDraft("a", "#2");
    const latest = store().startAttempt("a")!;
    store().setDraft("a", "#3");

    expect(store().finishAttempt("a", stale, "https://github.com/acme/app/pull/1")).toBe(false);
    expect(store().failAttempt("a", stale, "No access.")).toBe(false);
    expect(store().attempts.a).toEqual({ id: latest, status: "pending", draft: "#2" });
    expect(store().drafts.a).toBe("#3");
    expect(store().lastAttached).toBeNull();
  });
});

describe("linkPullRequestAttempt", () => {
  it("says what became of this attempt: linked, failed or busy", async () => {
    usePullRequestAttachStore.setState({ attempts: {}, drafts: {}, lastAttached: null });
    await expect(
      linkPullRequestAttempt("a", "https://github.com/acme/app/pull/1", async () => undefined),
    ).resolves.toEqual({ status: "linked", url: "https://github.com/acme/app/pull/1" });

    const pending = deferred();
    const first = linkPullRequestAttempt("a", "https://github.com/acme/app/pull/2", () =>
      pending.promise.then(() => {
        throw new Error("GitHub said no.");
      }),
    );
    await expect(
      linkPullRequestAttempt("a", "https://github.com/acme/app/pull/3", async () => undefined),
    ).resolves.toEqual({ status: "busy" });
    pending.resolve(undefined);
    await expect(first).resolves.toEqual({ status: "failed", message: "GitHub said no." });
  });

  it("records a failure only on the attempt it belongs to", async () => {
    usePullRequestAttachStore.setState({ attempts: {}, drafts: {}, lastAttached: null });
    const pending = deferred();
    const stale = linkPullRequestAttempt("a", "https://github.com/acme/app/pull/1", () =>
      pending.promise.then(() => {
        throw new Error("Too late.");
      }),
    );
    // A newer attempt takes over the session, as the picker does after a failure.
    const attemptId = usePullRequestAttachStore.getState().attempts.a!.id;
    usePullRequestAttachStore.getState().failAttempt("a", attemptId, "Replaced.");
    usePullRequestAttachStore.setState({ attempts: {} });
    const newer = usePullRequestAttachStore.getState().startAttempt("a");
    pending.resolve(undefined);
    await expect(stale).resolves.toEqual({ status: "failed", message: "Too late." });
    expect(store().attempts.a).toMatchObject({ id: newer, status: "pending" });
  });
});

describe("pullRequestLinkState", () => {
  const PR = "https://github.com/acme/app/pull/7";

  it("follows one PR's own link from pending to failed, however its link is written", async () => {
    const pending = deferred();
    const outcome = linkPullRequestAttempt("conv_1", PR, () => pending.promise);
    expect(
      pullRequestLinkState(store().attempts.conv_1, "https://GitHub.com/acme/app/pull/7/"),
    ).toEqual({ status: "pending" });
    pending.reject(new Error("GitHub said no."));
    await outcome;
    expect(pullRequestLinkState(store().attempts.conv_1, PR)).toEqual({
      status: "failed",
      message: "GitHub said no.",
    });
  });

  it("leaves other PRs, and the same PR in another session, idle", () => {
    void linkPullRequestAttempt("conv_1", PR, () => new Promise(() => {}));
    expect(
      pullRequestLinkState(store().attempts.conv_1, "https://github.com/acme/app/pull/8"),
    ).toEqual({ status: "idle" });
    expect(pullRequestLinkState(store().attempts.conv_2, PR)).toEqual({ status: "idle" });
  });

  it("refuses a second link for the session while one runs, from any entry point", async () => {
    void linkPullRequestAttempt("conv_1", PR, () => new Promise(() => {}));
    await expect(linkPullRequestAttempt("conv_1", PR, () => Promise.resolve(PR))).resolves.toEqual({
      status: "busy",
    });
  });
});
