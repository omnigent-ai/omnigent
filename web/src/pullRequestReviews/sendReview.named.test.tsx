// A review as the Pull Requests page's own login: at the send it must still go as the account
// the menu showed, under the same sign-in, or nothing is sent and the draft stays. Check Again
// after an unconfirmed one settles only from an answer read under the sign-in it began with.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { hostFetch } = vi.hoisted(() => ({ hostFetch: vi.fn() }));
// The signed-in workspace user, whom a test can switch.
const workspaceUser = vi.hoisted(() => ({ id: "user-1" }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => workspaceUser.id,
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));

import {
  draftOwner,
  MAC_ACTOR_CHANGED,
  macWriteRoute,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  BOB,
  HOST_LOGIN,
  installNamedWriteShell,
  removeNamedWriteShell,
} from "@/test/macNamedWriteShell";

import { readReviewSubmit, resetReviewSubmitForTests, reviewSubmitKey } from "./reviewSubmit";
import { sendReview } from "./sendReview";
import { useReviewSubmit } from "./useReviewSubmit";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "pull-requests-page";
const HEAD = "a".repeat(40);
const reviewCheck = () => ({
  object: "session.github.pr_review_check",
  head_sha: HEAD,
  author: "carol",
  pending: false,
  reviews: [],
});

/** Nothing reached GitHub: neither write method, nor the session's machine. */
function expectNothingSent(shell: ReturnType<typeof installNamedWriteShell>) {
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(hostFetch).not.toHaveBeenCalled();
}

/** Send a commented review with the route the menu rendered with. */
function sendAs(route: NonNullable<ReturnType<typeof macWriteRoute>>) {
  const key = reviewSubmitKey(draftOwner(), PR, route.actor.login);
  const onRefused = vi.fn();
  const sent = sendReview({
    key,
    route,
    prUrl: PR,
    event: "COMMENT",
    body: "Looks good",
    headSha: HEAD,
    origin: null,
    queryClient: new QueryClient(),
    conversationId: SESSION,
    onRefused,
  });
  return { key, sent, onRefused };
}

afterEach(() => {
  workspaceUser.id = "user-1";
  hostFetch.mockReset();
  setMacWritesReleaseFlagForTests(null);
  resetReviewSubmitForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("submitting a review the menu showed as the page's own login", () => {
  it("sends nothing once the login signed in again since the menu rendered", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    const rendered = macWriteRoute(PR, BOB)!;
    shell.bobSignsInAgain(2);
    await shell.readAsBob(PR);
    expect(macWriteRoute(PR, BOB)?.actor.generation).toBe(2);
    const { key, sent } = sendAs(rendered);
    expect(sent).toBe(false);
    expect(readReviewSubmit(key).status).toMatchObject({
      kind: "refused",
      code: "account_changed",
      message: MAC_ACTOR_CHANGED,
    });
    expect(readReviewSubmit(key).body).toBe("Looks good");
    expectNothingSent(shell);
  });

  it("sends as that login when nothing changed since the menu rendered", async () => {
    const shell = installNamedWriteShell(() => ({ ok: false }));
    await shell.ready(PR);
    const { sent } = sendAs(macWriteRoute(PR, BOB)!);
    expect(sent).toBe(true);
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({ macLogin: "bob" });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });
});

describe("Check Again for a review GitHub didn't confirm", () => {
  const ALICE = { host: "github.com", login: HOST_LOGIN };
  const landed = () => ({
    ...reviewCheck(),
    reviews: [
      {
        id: "R_1",
        state: "COMMENTED",
        body: "Looks good",
        created_at: new Date().toISOString(),
        commit_oid: HEAD,
      },
    ],
  });
  const unknownOutcome = () => ({
    ok: false,
    error: { code: "outcome_unknown", message: "GitHub didn’t answer." },
    known_review_ids: [],
  });

  /** Alice, as gh's active account and by name, both at generation 1. */
  async function aliceTwice() {
    const shell = installNamedWriteShell(unknownOutcome, {
      bobGeneration: 1,
      reads: { pull_request_review_check: landed },
    });
    await shell.ready(PR);
    await shell.readNamed(PR, ALICE);
    return shell;
  }

  /** Send as `route`, which GitHub doesn't confirm; the hold's key. */
  async function sendUnconfirmed(route: NonNullable<ReturnType<typeof macWriteRoute>>) {
    const { key } = sendAs(route);
    await waitFor(() => expect(readReviewSubmit(key).status.kind).toBe("uncertain"));
    return key;
  }

  /** The Review menu of a view showing `route`: the page's, or a session panel's. */
  function menuFor(route: NonNullable<ReturnType<typeof macWriteRoute>>) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return renderHook(
      () =>
        useReviewSubmit({
          conversationId: SESSION,
          pr: { url: PR, number: 42, state: "OPEN", author: "carol", headSha: HEAD },
          offer: { kind: "mac", route },
        }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    ).result;
  }

  const checkReads = (shell: ReturnType<typeof installNamedWriteShell>) =>
    shell.codeHostQuery.mock.calls
      .map(([request]) => request)
      .filter((request) => request.operation === "pull_request_review_check");

  it("stays held, reading nothing, once its scope can't be read as the login", async () => {
    const shell = await aliceTwice();
    const key = await sendUnconfirmed(macWriteRoute(PR, ALICE)!);
    await shell.setWriteAs(false);
    const sessionMenu = menuFor(macWriteRoute(PR, null)!);
    act(() => sessionMenu.current.checkAgain());
    await waitFor(() =>
      expect(readReviewSubmit(key).status).toMatchObject({
        kind: "uncertain",
        checked: "unreadable",
      }),
    );
    expect(checkReads(shell)).toEqual([]);
  });
});
