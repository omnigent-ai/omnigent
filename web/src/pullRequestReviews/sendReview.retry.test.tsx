// A bulk run's approval sent again after GitHub refused it for a passing reason, before applying
// anything: each send on a fresh ticket, still submitting while it waits, and only its last answer
// kept. An approval GitHub may have made is never sent again: a second would be another review.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient } from "@tanstack/react-query";
import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import {
  draftOwner,
  macWriteRoute,
  REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  setReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { installMacWriteShell, MAC_IDENTITY } from "@/test/macWriteShell";

import { readReviewSubmit, resetReviewSubmitForTests, reviewSubmitKey } from "./reviewSubmit";
import { sendReview } from "./sendReview";

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const refused = (code: string, cause?: string) => ({
  ok: false,
  error: { code, message: `GitHub said ${code}.`, ...(cause ? { cause } : {}) },
});
const submitted = {
  ok: true,
  data: {
    object: "session.github.pr_review",
    pr_url: PR,
    review: {
      id: "PRR_1",
      state: "APPROVED",
      body: "",
      created_at: "2026-11-13T12:00:00Z",
      url: `${PR}#pullrequestreview-1`,
      commit_oid: HEAD,
    },
  },
  actor: { login: MAC_IDENTITY.login },
};

/** Approve as a bulk run would, answering each send in turn from `answers`. */
async function approve(answers: unknown[], mayRetry: () => boolean = () => true) {
  const shell = await installMacWriteShell(
    PR,
    async () => answers.shift() ?? submitted,
    {},
    {
      extraWrites: REVIEW_WRITE_OPERATIONS,
    },
  );
  setReviewsFlagForTests(true);
  const route = macWriteRoute(PR, null)!;
  const key = reviewSubmitKey(draftOwner(), PR, route.actor.login);
  const onApplied = vi.fn();
  const onRefused = vi.fn();
  const onUncertain = vi.fn();
  const waits: number[] = [];
  const sent = sendReview({
    key,
    route,
    prUrl: PR,
    event: "APPROVE",
    body: "",
    headSha: HEAD,
    origin: null,
    queryClient: new QueryClient(),
    conversationId: "pull-requests-page",
    onApplied,
    onRefused,
    onUncertain,
    retry: {
      mayRetry,
      sleep: async (ms) => {
        waits.push(ms);
        // Still submitting while it waits: nothing says it was refused yet.
        expect(readReviewSubmit(key).status.kind).toBe("submitting");
      },
    },
  });
  expect(sent).toBe(true);
  const settled = () =>
    onApplied.mock.calls.length + onRefused.mock.calls.length + onUncertain.mock.calls.length;
  await waitFor(() => expect(settled()).toBe(1));
  return { shell, key, onApplied, onRefused, onUncertain, waits };
}

afterEach(() => {
  setMacWritesReleaseFlagForTests(null);
  setReviewsFlagForTests(null);
  resetReviewSubmitForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("a bulk run's approval, sent again after a passing refusal", () => {
  it("approves on its second send once GitHub's rate limit lets it", async () => {
    const { shell, onApplied, onRefused, waits } = await approve([refused("rate_limited")]);
    expect(shell.write).toHaveBeenCalledTimes(2);
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(onRefused).not.toHaveBeenCalled();
    expect(waits).toHaveLength(1);
    // Each send its own request, never the last one's.
    const [first, second] = shell.write.mock.calls.map(
      ([request]) => (request as { requestId: string }).requestId,
    );
    expect(first).not.toBe(second);
  });

  it("sends again after a check that timed out, but never after an SSO refusal", async () => {
    const timedOut = await approve([refused("query_failed", "timeout")]);
    expect(timedOut.shell.write).toHaveBeenCalledTimes(2);
    expect(timedOut.onApplied).toHaveBeenCalledTimes(1);
    resetReviewSubmitForTests();
    const sso = await approve([refused("query_failed", "sso_required")]);
    expect(sso.shell.write).toHaveBeenCalledTimes(1);
    expect(sso.onRefused).toHaveBeenCalledWith("query_failed", expect.any(String));
  });

  it("never sends an approval GitHub may have made again", async () => {
    const { shell, key, onUncertain } = await approve([refused("outcome_unknown")]);
    expect(shell.write).toHaveBeenCalledTimes(1);
    expect(onUncertain).toHaveBeenCalledWith({ abandoned: false });
    expect(readReviewSubmit(key).status.kind).toBe("uncertain");
  });

  it("keeps the last refusal after three retries, or once the run no longer allows one", async () => {
    const limited = await approve(Array.from({ length: 4 }, () => refused("rate_limited")));
    expect(limited.shell.write).toHaveBeenCalledTimes(4);
    expect(limited.onRefused).toHaveBeenCalledWith("rate_limited", expect.any(String));
    expect(readReviewSubmit(limited.key).status).toMatchObject({
      kind: "refused",
      code: "rate_limited",
    });
    resetReviewSubmitForTests();
    const ended = await approve([refused("rate_limited")], () => false);
    expect(ended.shell.write).toHaveBeenCalledTimes(1);
    expect(ended.onRefused).toHaveBeenCalledTimes(1);
  });

  it("never sends a refusal that would only be refused again", async () => {
    const { shell, onRefused } = await approve([refused("head_changed")]);
    expect(shell.write).toHaveBeenCalledTimes(1);
    expect(onRefused).toHaveBeenCalledWith("head_changed", expect.any(String));
  });
});
