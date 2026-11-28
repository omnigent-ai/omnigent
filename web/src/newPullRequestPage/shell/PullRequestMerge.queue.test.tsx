// The Merge section's merge queue through the session's host: a base that merges through a
// queue offers Add to Merge Queue, pinned to the shown head; a queued PR shows where it stands
// and offers Remove from Queue. Where the host doesn't serve the queue, the section merges.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import type { PullRequest } from "@/newPullRequestPage/hooks/usePullRequests";

const host = vi.hoisted(() => ({
  served: new Set<string>(),
  read: vi.fn(),
  write: vi.fn(),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) => host.served.has(method),
  hostChangeRoute: (conversationId: string, method: string) =>
    host.served.has(method) ? { conversationId, key: "route-1" } : null,
}));
// The workspace user, which a test switches while a change's answer is outstanding.
const workspaceUser = vi.hoisted(() => ({ id: "user-a" }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => workspaceUser.id,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
  writePullRequestRpc: host.write,
}));

import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import {
  holdPullRequestStateChanges,
  resetPullRequestStateHoldsForTests,
  sendStateChange,
  unconfirmedStateChangeHolds,
} from "@/lib/pullRequestStateHolds";
import { mergeQueueKey } from "@/newPullRequestPage/hooks/useMergeQueue";

import { PullRequestMerge } from "./PullRequestMerge";

const PR_URL = "https://github.com/acme/app/pull/7";
const ENTERPRISE_PR_URL = "https://ghe.example.com/acme/app/pull/7";
const HEAD = "a".repeat(40);
const QUEUE_METHODS = ["getMergeQueue", "enqueuePullRequest", "dequeuePullRequest"];

function pullRequest(url: string = PR_URL): PullRequest {
  return {
    number: 7,
    title: "Ship the widget",
    state: "OPEN",
    url,
    is_draft: false,
    author: "dev",
    base_ref: "main",
    head_ref: "widget",
    head_sha: HEAD,
    checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    merge_state_status: "CLEAN",
    auto_merge: null,
  };
}

function queueAnswer(over: Record<string, unknown> = {}) {
  return {
    object: "session.github.merge_queue",
    pr_url: PR_URL,
    enabled: true,
    queued: false,
    position: null,
    state: null,
    head_sha: HEAD,
    ...over,
  };
}

/** Answer each host read by method: the queue's state, and the repository's merge options. */
function answerReads(queue: unknown) {
  host.read.mockImplementation(async (_conversationId: string, method: string) =>
    method === "getMergeQueue"
      ? queue
      : { object: "session.github.merge_options", methods: ["squash"], default: "squash" },
  );
}

/** The method and request of each host write sent so far. */
async function sent(): Promise<[string, unknown][]> {
  const requests: [string, unknown][] = [];
  const workspace = new Proxy(
    {},
    {
      get: (_target, method: string) => async (request: unknown) => {
        requests.push([method, request]);
        return { payload: {} };
      },
    },
  ) as unknown as WorkspaceClient;
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
  return requests;
}

function renderMerge(prUrl: string = PR_URL) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PullRequestMerge conversationId="conv_1" prUrl={prUrl} pr={pullRequest(prUrl)} />
    </QueryClientProvider>,
  );
  return client;
}

/** Press Add to Merge Queue, then confirm in the dialog it opens. */
async function addAndConfirm() {
  fireEvent.click(await screen.findByRole("button", { name: "Add to Merge Queue" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Add to Merge Queue" }));
  return dialog;
}

beforeEach(() => {
  workspaceUser.id = "user-a";
  host.served = new Set(["getMergeOptions"]);
  host.read.mockReset();
  host.write.mockReset();
});
afterEach(() => resetPullRequestStateHoldsForTests());

describe("a base that merges through a queue, where the host serves it", () => {
  it("adds the PR to the queue at the shown head, and shows where it stands", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer());
    host.write.mockResolvedValue(queueAnswer({ queued: true, position: 2, state: "QUEUED" }));
    renderMerge();
    await addAndConfirm();
    expect(await screen.findByText("In the merge queue · Position 2 · Queued")).toBeInTheDocument();
    await expect(sent()).resolves.toEqual([
      ["enqueuePullRequest", { prUrl: PR_URL, expectedHeadOid: HEAD }],
    ]);
  });

  it("asks before adding, naming the PR, its base branch and the only allowed method", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer());
    renderMerge();
    fireEvent.click(await screen.findByRole("button", { name: "Add to Merge Queue" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Add pull request #7 to the merge queue?")).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      "The merge queue merges Ship the widget into main with squash once its checks pass.",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(host.write).not.toHaveBeenCalled();
  });

  it("names no method when the repository allows several, since the queue picks its own", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    host.read.mockImplementation(async (_conversationId: string, method: string) =>
      method === "getMergeQueue"
        ? queueAnswer()
        : {
            object: "session.github.merge_options",
            methods: ["merge", "squash"],
            default: "squash",
          },
    );
    renderMerge();
    fireEvent.click(await screen.findByRole("button", { name: "Add to Merge Queue" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "into main with the queue’s merge method once its checks pass.",
    );
  });

  it.each([
    ["github.com", PR_URL],
    ["a GitHub Enterprise host", ENTERPRISE_PR_URL],
  ])(
    "sends nothing while another state change to the PR on %s, such as a close, is in flight",
    async (_host, prUrl) => {
      host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
      answerReads(queueAnswer({ pr_url: prUrl }));
      const release = holdPullRequestStateChanges(prUrl);
      renderMerge(prUrl);
      const dialog = await addAndConfirm();
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "This pull request is already being changed. Wait for it to finish.",
      );
      expect(host.write).not.toHaveBeenCalled();
      release();
    },
  );

  it("removes a queued PR from the queue", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer({ queued: true, position: 1, state: "AWAITING_CHECKS" }));
    host.write.mockResolvedValue(queueAnswer());
    renderMerge();
    expect(
      await screen.findByText("In the merge queue · Position 1 · Waiting for checks"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove from Queue" }));
    expect(await screen.findByRole("button", { name: "Add to Merge Queue" })).toBeInTheDocument();
    await expect(sent()).resolves.toEqual([["dequeuePullRequest", { prUrl: PR_URL }]]);
  });

  it("says GitHub's refusal, and that an add may have been made when the answer is lost", async () => {
    const { PullRequestApiError } = await import("@/lib/pullRequestRpc");
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer());
    host.write.mockRejectedValueOnce(
      new PullRequestApiError("This pull request is already in the merge queue.", 400),
    );
    renderMerge();
    const dialog = await addAndConfirm();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "This pull request is already in the merge queue.",
    );
    host.write.mockRejectedValueOnce(new PullRequestMaybeSentError(new TypeError("dropped")));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add to Merge Queue" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "The app couldn’t tell whether GitHub added this pull request to the merge queue.",
      ),
    );
  });

  it("holds the PR's other state changes after a lost add, until a queue read answers", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer());
    const client = renderMerge();
    await screen.findByRole("button", { name: "Add to Merge Queue" });
    // The read that checks the add waits, then fails.
    const queueReads: { resolve: (answer: unknown) => void; reject: (error: Error) => void }[] = [];
    host.read.mockImplementation((_conversationId: string, method: string) =>
      method === "getMergeQueue"
        ? new Promise((resolve, reject) => {
            queueReads.push({ resolve, reject });
          })
        : Promise.resolve({ object: "session.github.merge_options", methods: ["squash"] }),
    );
    host.write.mockRejectedValueOnce(new PullRequestMaybeSentError(new TypeError("dropped")));
    await addAndConfirm();
    await waitFor(() => expect(queueReads).toHaveLength(1));

    const merge = vi.fn(async () => "merged");
    const refusedWhileHeld = async () => {
      expect(unconfirmedStateChangeHolds(PR_URL, "pull_request_close")).toBe(true);
      await expect(sendStateChange(PR_URL, "pull_request_merge", merge)).rejects.toMatchObject({
        code: "unconfirmed",
      });
    };
    await refusedWhileHeld();
    await act(async () => queueReads[0]!.reject(new Error("offline")));
    await refusedWhileHeld();
    expect(merge).not.toHaveBeenCalled();

    void client.invalidateQueries({ queryKey: mergeQueueKey("conv_1", PR_URL) });
    await waitFor(() => expect(queueReads).toHaveLength(2));
    await act(async () => queueReads[1]!.resolve(queueAnswer({ queued: true, position: 1 })));
    await waitFor(() =>
      expect(unconfirmedStateChangeHolds(PR_URL, "pull_request_close")).toBe(false),
    );
    await expect(sendStateChange(PR_URL, "pull_request_merge", merge)).resolves.toBe("merged");
  });

  it("keeps a lost add's hold with the user who sent it, though another signed in meanwhile", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads(queueAnswer());
    renderMerge();
    await screen.findByRole("button", { name: "Add to Merge Queue" });
    let loseAnswer: (error: Error) => void = () => {};
    host.write.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          loseAnswer = reject;
        }),
    );
    await addAndConfirm();
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));

    // User A sent the add; B is signed in by the time its answer is lost.
    workspaceUser.id = "user-b";
    await act(async () => loseAnswer(new PullRequestMaybeSentError(new TypeError("dropped"))));
    expect(unconfirmedStateChangeHolds(PR_URL, "pull_request_close")).toBe(false);
    workspaceUser.id = "user-a";
    expect(unconfirmedStateChangeHolds(PR_URL, "pull_request_close")).toBe(true);
  });

  it("shows a queued PR without Remove where the host serves only the read", async () => {
    host.served = new Set(["getMergeOptions", "getMergeQueue"]);
    answerReads(queueAnswer({ queued: true, position: 3, state: "QUEUED" }));
    renderMerge();
    expect(await screen.findByText("In the merge queue · Position 3 · Queued")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove from Queue" })).toBeNull();
  });
});

describe("the merge queue where the host doesn't serve it", () => {
  it("reads no queue and merges as before", async () => {
    answerReads(queueAnswer({ queued: true, position: 1 }));
    renderMerge();
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeInTheDocument();
    expect(host.read.mock.calls.map(([, method]) => method)).not.toContain("getMergeQueue");
    expect(screen.queryByText(/In the merge queue/)).toBeNull();
  });

  it("treats an answer it can't read as no queue", async () => {
    host.served = new Set(["getMergeOptions", ...QUEUE_METHODS]);
    answerReads({ object: "session.github.merge_queue", pr_url: PR_URL, enabled: "yes" });
    renderMerge();
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add to Merge Queue" })).toBeNull();
  });
});
