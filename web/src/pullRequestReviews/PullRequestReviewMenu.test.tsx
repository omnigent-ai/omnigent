// The Review menu with the real read-path and write-path stores over a stand-in shell: what it
// offers on each shell and flag, what a submit sends, how refusals and unconfirmed submits are
// shown and settled, that a summary never follows into another Mac account, and ⌘K's rows.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    loading: vi.fn(() => "progress"),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: vi.fn(async () => new Response("{}", { status: 500 })),
}));

import { gitProviderCopy } from "@/lib/gitProviders";
import {
  type PullRequestActionOrigin,
  PullRequestActionOriginContext,
} from "@/lib/pullRequestActionOrigin";
import {
  GITHUB_WRITE_SET_V1,
  REVIEW_WRITE_OPERATIONS,
  beginMacWrite,
  macWriteInFlight,
  macWriteRoute,
  parseReplyAnswer,
  resetDesktopPullRequestWritesForTests,
  sendMacWrite,
  setMacWritesReleaseFlagForTests,
  setPageReviewOperationsForTests,
  setPageWriteOperationsForTests,
  setReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import type { PullRequestDetailsState } from "@/pullRequestPage/detailsLayout";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AVAILABLE, usePaletteCommandsStore } from "@/store/paletteCommands";

import { toast } from "sonner";

import { PullRequestDestinationContext } from "@/pullRequestPage/pullRequestDestination";

import { PullRequestReviewMenu } from "./PullRequestReviewMenu";
import {
  readReviewSubmit,
  REVIEW_MENU_REQUEST_WAIT_MS,
  requestReviewMenu,
  resetReviewSubmitForTests,
  reviewSubmitKey,
  updateReviewSubmit,
} from "./reviewSubmit";
import { APPROVAL_SOURCES, type ApprovalSource } from "./approvalSources";
import { approveNow, approveRetryFor, type ApproveNowOptions } from "./sendReview";
import type { ReviewerPingAction, ReviewerPingPullRequest } from "@/hooks/useReviewerPings";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const VIEWER = "mac-octo";
const REVIEW_WRITES = {
  version: 1,
  operations: [...GITHUB_WRITE_SET_V1, ...REVIEW_WRITE_OPERATIONS, "pull_request_review_check"],
};

type WriteAnswer = Record<string, unknown>;

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

/** What the check read answers, as the shell's `codeHostQuery` would, read as `VIEWER`. */
let checkAnswer: Record<string, unknown>;
const queryAnswer = (data: unknown, login = VIEWER) => ({
  ok: true,
  data,
  identity: { host: "github.com", login, generation: 1 },
});
const check = (fields: Record<string, unknown> = {}) => ({
  object: "session.github.pr_review_check",
  head_sha: HEAD,
  author: "author-octo",
  pending: false,
  reviews: [],
  ...fields,
});

/** A shell that reads GitHub as `VIEWER` on this Mac and lists `writes`. */
async function readyShell(
  writes: unknown,
  codeHostWrite = vi.fn<(r: unknown) => Promise<WriteAnswer>>(),
) {
  const codeHostQuery = vi.fn(async (request: { operation: string }) =>
    request.operation === "pull_request_review_check"
      ? queryAnswer(checkAnswer)
      : { ok: true, data: [] },
  );
  installShell({
    codeHostQuery,
    codeHostWrite,
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_review_check"],
          accounts: true,
          writes,
        },
      ],
    }),
  });
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  return { codeHostWrite, codeHostQuery };
}

function readAs(login: string, generation = 1) {
  expect(
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login, generation },
    }),
  ).toBe("accepted");
}

/** This Mac now reads, and would write, as `login`: the first answer moves the scope on. */
function switchMacAccount(login: string, generation: number) {
  for (let read = 0; read < 2; read += 1) {
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login, generation },
    });
  }
}

/** Everything the review route needs: the whole set's route, the shell's list, the flag. */
async function reviewable(codeHostWrite?: ReturnType<typeof vi.fn>) {
  const shell = await readyShell(REVIEW_WRITES, codeHostWrite as never);
  readAs(VIEWER);
  setMacWritesReleaseFlagForTests(true);
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  setReviewsFlagForTests(true);
  return shell;
}

function state(fields: Partial<PullRequestDetailsState["pr"]> = {}): PullRequestDetailsState {
  return {
    conversationId: "conv_1",
    copy: gitProviderCopy("github"),
    repository: "acme/app",
    pr: {
      number: 42,
      url: PR,
      state: "OPEN",
      isDraft: false,
      title: "Retry uploads",
      author: "author-octo",
      ...fields,
    },
    lifecycle: { headSha: HEAD },
    active: true,
  } as unknown as PullRequestDetailsState;
}

function show(
  fields: Partial<PullRequestDetailsState["pr"]> = {},
  origin: PullRequestActionOrigin | null = null,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestActionOriginContext.Provider value={origin}>
          <PullRequestReviewMenu state={state(fields)} />
        </PullRequestActionOriginContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const paletteRows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const row = (label: string) => paletteRows().find((command) => command.label === label);

async function openMenu() {
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
  return screen.findByRole("dialog", { name: "Submit review" });
}

const summary = () => screen.getByRole("textbox", { name: "Review summary" });
const submitted = (fields: Record<string, unknown> = {}): WriteAnswer => ({
  ok: true,
  data: {
    object: "session.github.pr_review",
    pr_url: PR,
    review: {
      id: "PRR_new",
      state: "APPROVED",
      body: "Ship it",
      created_at: "2026-11-08T10:00:00Z",
      url: `${PR}#pullrequestreview-1`,
      commit_oid: HEAD,
      ...fields,
    },
  },
  actor: { login: VIEWER },
});

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  checkAnswer = check();
  resetDesktopPullRequestWritesForTests();
  resetReviewSubmitForTests();
  usePaletteCommandsStore.setState({ groups: {} });
});

afterEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetReviewSubmitForTests();
  installShell(undefined);
  githubMacReadsPreference.write("automatic");
});

describe("what the Review menu offers", () => {
  it("offers Review on GitHub where this shell can't submit reviews, and sends nothing", async () => {
    const { codeHostWrite } = await readyShell({
      version: 1,
      operations: [...GITHUB_WRITE_SET_V1],
    });
    readAs(VIEWER);
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setReviewsFlagForTests(true);
    show();
    // The header keeps its room: no button, and ⌘K's row opens the PR on GitHub.
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(row("Review on GitHub")).toBeDefined();
    expect(row("Submit Review…")).toBeUndefined();
    expect(codeHostWrite).not.toHaveBeenCalled();
  });

  it("offers Review on GitHub while the review flag is off, though the shell lists the write", async () => {
    await reviewable();
    setReviewsFlagForTests(false);
    show();
    expect(row("Review on GitHub")).toBeDefined();
    expect(row("Submit Review…")).toBeUndefined();
  });

  it("lets a requested reviewer approve or request changes once the review flag turns on", async () => {
    await reviewable();
    setReviewsFlagForTests(false);
    show({ reviewers: [{ login: VIEWER.toUpperCase(), kind: "user", state: "PENDING" }] });
    expect(row("Review on GitHub")).toBeDefined();
    expect(row("Approve Pull Request…")).toBeUndefined();
    act(() => setReviewsFlagForTests(true));
    expect(row("Review on GitHub")).toBeUndefined();
    for (const label of ["Approve Pull Request…", "Request Changes…"]) {
      expect(row(label)).toBeDefined();
      expect(row(label)?.availability).toBeUndefined();
    }
    const menu = await openMenu();
    expect(within(menu).getByRole("radio", { name: /Approve/ })).toBeEnabled();
    expect(within(menu).getByRole("radio", { name: /Request changes/i })).toBeEnabled();
  });

  it("offers Review on GitHub to an older page on a newer shell", async () => {
    await reviewable();
    setPageReviewOperationsForTests([]);
    show();
    expect(row("Review on GitHub")).toBeDefined();
  });

  it("names GitSpoke for a GitSpoke pull request this Mac can't review", () => {
    const spoke = "https://2.spoke.example.com/example-org/universe/pull/7";
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestReviewMenu
            state={
              {
                ...state({ url: spoke }),
                copy: gitProviderCopy("gitspoke"),
              } as PullRequestDetailsState
            }
          />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(row("Review on GitSpoke")).toBeDefined();
    expect(row("Review on GitHub")).toBeUndefined();
  });

  it("offers Review on GitHub where no shell is there at all", () => {
    show();
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(row("Review on GitHub")).toBeDefined();
  });

  it("offers nothing on a merged or closed pull request", async () => {
    await reviewable();
    const { unmount } = show({ state: "MERGED" });
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(paletteRows()).toEqual([]);
    unmount();
    show({ state: "CLOSED" });
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
  });

  it("adds no ⌘K rows while the panel isn't the one shown", async () => {
    await reviewable();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestReviewMenu state={{ ...state(), active: false }} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument();
    expect(paletteRows()).toEqual([]);
  });
});

describe("submitting a review on this Mac", () => {
  it("submits the summary and verdict pinned to the shown head, then closes and starts over", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    expect(within(dialog).getByText(HEAD.slice(0, 7))).toBeInTheDocument();
    fireEvent.change(summary(), { target: { value: "Ship it" } });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Approve/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
    const [request] = codeHostWrite.mock.calls[0] as unknown as [Record<string, unknown>];
    expect(request).toMatchObject({
      provider: "github",
      host: "github.com",
      actor: { login: VIEWER, generation: 1 },
      operation: "pull_request_review_submit",
      params: {
        repository: "acme/app",
        number: 42,
        event: "APPROVE",
        body: "Ship it",
        expected_head: HEAD,
      },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const reopened = await openMenu();
    expect(summary()).toHaveValue("");
    expect(within(reopened).getByRole("radio", { name: /Comment/ })).toBeChecked();
  });

  // ⌘↵ is Ctrl+↵ off macOS, as here: the chord the summary's editor offers.
  const modEnter = { key: "Enter", ctrlKey: true };

  it("submits with ⌘↵ while a verdict has focus, as Submit Review does", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    const approve = within(dialog).getByRole("radio", { name: /Approve/ });
    fireEvent.click(approve);
    approve.focus();
    fireEvent.keyDown(approve, modEnter);
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
    const [request] = codeHostWrite.mock.calls[0] as unknown as [Record<string, unknown>];
    expect(request).toMatchObject({ params: { event: "APPROVE" } });
  });

  it("submits once with ⌘↵ from the summary or a button in the form", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "Ship it" } });
    // The summary's editor takes the chord itself; the form doesn't send it a second time.
    fireEvent.keyDown(summary(), modEnter);
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const reopened = await openMenu();
    fireEvent.change(summary(), { target: { value: "Again" } });
    fireEvent.keyDown(within(reopened).getByRole("button", { name: "Submit Review" }), modEnter);
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(2));
    expect(dialog.isConnected).toBe(false);
  });

  it.each(["the Write tab", "the Preview tab", "a formatting button"])(
    "submits once with ⌘↵ from %s of the summary",
    async (control) => {
      const codeHostWrite = vi.fn(async () => submitted());
      await reviewable(codeHostWrite);
      show();
      const dialog = await openMenu();
      fireEvent.change(summary(), { target: { value: "Ship it" } });
      const target =
        control === "the Write tab"
          ? within(dialog).getByRole("tab", { name: "Write" })
          : control === "the Preview tab"
            ? within(dialog).getByRole("tab", { name: "Preview" })
            : within(within(dialog).getByRole("toolbar", { name: "Formatting" })).getAllByRole(
                "button",
              )[0]!;
      target.focus();
      fireEvent.keyDown(target, modEnter);
      await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
      // Once: the summary's editor didn't take this key, so nothing sent it again.
      await act(async () => {
        await Promise.resolve();
      });
      expect(codeHostWrite).toHaveBeenCalledTimes(1);
    },
  );

  it("sends nothing on ⌘↵ while Submit Review can't run", async () => {
    const codeHostWrite = vi.fn();
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    // A comment needs a summary first.
    const comment = within(dialog).getByRole("radio", { name: /Comment/ });
    comment.focus();
    fireEvent.keyDown(comment, modEnter);
    fireEvent.keyDown(comment, { key: "Enter" });
    expect(codeHostWrite).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("button", { name: "Submit Review" })).toBeDisabled();
  });

  it("asks for a summary before a comment or a request for changes", async () => {
    const codeHostWrite = vi.fn();
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    const submit = within(dialog).getByRole("button", { name: "Submit Review" });
    expect(submit).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("radio", { name: /Request Changes/ }));
    expect(submit).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("radio", { name: /Approve/ }));
    expect(submit).toBeEnabled();
    expect(codeHostWrite).not.toHaveBeenCalled();
  });

  it("keeps the author from approving or requesting changes, in the menu and in ⌘K", async () => {
    await reviewable();
    show({ author: VIEWER.toUpperCase() });
    expect(row("Approve Pull Request…")?.availability).toEqual({
      available: false,
      reason: "You can’t approve your own pull request.",
    });
    expect(row("Request Changes…")?.availability).toEqual({
      available: false,
      reason: "You can’t do this on your own pull request.",
    });
    expect(row("Submit Review…")?.availability).toBeUndefined();
    const dialog = await openMenu();
    expect(within(dialog).getByRole("radio", { name: /Approve/ })).toBeDisabled();
    expect(within(dialog).getByRole("radio", { name: /Request Changes/ })).toBeDisabled();
    expect(
      within(dialog).getByText("You can’t approve your own pull request."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("You can’t do this on your own pull request."),
    ).toBeInTheDocument();
  });

  it("shows GitHub's refusal in place and keeps the summary", async () => {
    const codeHostWrite = vi.fn(async () => ({
      ok: false,
      error: {
        code: "head_changed",
        message: "The pull request has new commits. Look at them, then submit your review again.",
      },
    }));
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "Needs a retry limit" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("new commits");
    expect(summary()).toHaveValue("Needs a retry limit");
    // Changing what's sent clears the old message.
    fireEvent.change(summary(), { target: { value: "Needs a retry limit." } });
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  it("lets the person try again after GitHub refuses before applying it", async () => {
    const codeHostWrite = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        error: { code: "forbidden", message: "@mac-octo can't do this in acme/app." },
      })
      .mockResolvedValueOnce(submitted());
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "Ship it" } });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Approve/ }));
    const submit = within(dialog).getByRole("button", { name: "Submit Review" });
    fireEvent.click(submit);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("can't do this");
    // A definite no holds nothing: the text stays editable and Submit runs again.
    expect(summary()).toBeEnabled();
    expect(summary()).toHaveValue("Ship it");
    expect(row("Check Whether Your Review Was Submitted")).toBeUndefined();
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("says a review is in progress on GitHub and holds Submit until it's finished there", async () => {
    const codeHostWrite = vi.fn();
    checkAnswer = check({ pending: true });
    await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    expect(
      await within(dialog).findByText(/You have a review in progress on GitHub/),
    ).toBeInTheDocument();
    fireEvent.change(summary(), { target: { value: "LGTM" } });
    expect(within(dialog).getByRole("button", { name: "Submit Review" })).toBeDisabled();
    expect(codeHostWrite).not.toHaveBeenCalled();
  });

  it("warns when GitHub's head has moved past the one shown", async () => {
    checkAnswer = check({ head_sha: "c".repeat(40) });
    await reviewable();
    show();
    const dialog = await openMenu();
    expect(
      await within(dialog).findByText(/GitHub has commits this page doesn’t show yet/),
    ).toBeInTheDocument();
  });
});

describe("where the Pull Requests page shows a submitted review going", () => {
  const pageOrigin = () => {
    const succeeded = vi.fn();
    return { succeeded, origin: { begin: vi.fn(() => ({ succeeded })) } };
  };

  it("asks the page to show an approval sent from the menu land", async () => {
    await reviewable(vi.fn(async () => submitted()));
    const { origin, succeeded } = pageOrigin();
    show({}, origin);
    const dialog = await openMenu();
    fireEvent.click(within(dialog).getByRole("radio", { name: /Approve/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    await waitFor(() => expect(succeeded).toHaveBeenCalledWith("approved"));
    expect(origin.begin).toHaveBeenCalledExactlyOnceWith(
      PR,
      { login: VIEWER, generation: 1 },
      { headSha: HEAD, landing: "approved" },
    );
  });

  it.each([
    ["Comment", "commented"],
    ["Request Changes", "changes_requested"],
  ] as const)("asks for no landing for %s", async (verdict, action) => {
    await reviewable(vi.fn(async () => submitted({ state: action.toUpperCase() })));
    const { origin, succeeded } = pageOrigin();
    show({}, origin);
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "Thoughts" } });
    fireEvent.click(within(dialog).getByRole("radio", { name: new RegExp(verdict) }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    await waitFor(() => expect(succeeded).toHaveBeenCalled());
    expect(origin.begin).toHaveBeenCalledExactlyOnceWith(
      PR,
      { login: VIEWER, generation: 1 },
      { headSha: HEAD },
    );
  });
});

describe("one review change per pull request", () => {
  it("holds Submit while a reply to the same pull request is on its way", async () => {
    const codeHostWrite = vi.fn(() => new Promise<WriteAnswer>(() => {}));
    await reviewable(codeHostWrite);
    const route = macWriteRoute(PR, null)!;
    void sendMacWrite(beginMacWrite(route), "pull_request_review_reply", {}, parseReplyAnswer);
    expect(macWriteInFlight("pull_request_review_submit", route.target)).toBe(true);
    expect(macWriteInFlight("pull_request_review_thread_create", route.target)).toBe(true);
    expect(macWriteInFlight("pull_request_comment", route.target)).toBe(false);
    show();
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "LGTM" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("still being sent");
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });
});

describe("a submit GitHub didn't confirm", () => {
  /** Submits "Ship it" as an approval GitHub doesn't confirm; `knownIds: false` knew no reviews before. */
  async function unconfirmed({ knownIds = true }: { knownIds?: boolean } = {}) {
    const codeHostWrite = vi.fn(async () => ({
      ok: false,
      error: {
        code: "outcome_unknown",
        message:
          "GitHub didn't confirm your review. Check the pull request before submitting again.",
      },
      ...(knownIds ? { known_review_ids: ["PRR_old"] } : {}),
    }));
    const shell = await reviewable(codeHostWrite);
    show();
    const dialog = await openMenu();
    fireEvent.change(summary(), { target: { value: "Ship it" } });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Approve/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    await within(dialog).findByText(/didn't confirm your review/);
    return { dialog, ...shell };
  }
  const newReview = (fields: Record<string, unknown> = {}) => ({
    id: "PRR_new",
    state: "APPROVED",
    body: "Ship it",
    // Made just now, after the click.
    created_at: new Date().toISOString(),
    commit_oid: HEAD,
    ...fields,
  });

  it("holds the text and every submit, in the menu and in ⌘K, and never sends again", async () => {
    const { dialog, codeHostWrite } = await unconfirmed();
    expect(summary()).toHaveValue("Ship it");
    expect(summary()).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Submit Review" })).toBeDisabled();
    expect(row("Submit Review…")?.availability).toMatchObject({ available: false });
    expect(row("Check Whether Your Review Was Submitted")).toBeDefined();
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("stays held when the check finds only a review from before, or an unrelated one", async () => {
    const { dialog } = await unconfirmed();
    checkAnswer = check({
      reviews: [
        newReview({ id: "PRR_old" }),
        newReview({ id: "PRR_other", state: "COMMENTED" }),
        newReview({ id: "PRR_moved", commit_oid: "c".repeat(40) }),
        newReview({ id: "PRR_words", body: "Something else" }),
        // Same verdict, commit and words, but from long before this submit.
        newReview({ id: "PRR_ancient", created_at: "2020-01-01T00:00:00Z" }),
      ],
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check Again" }));
    expect(await within(dialog).findByText(/doesn’t show on GitHub yet/)).toBeInTheDocument();
    expect(summary()).toHaveValue("Ship it");
    expect(within(dialog).getByRole("button", { name: "Submit Review" })).toBeDisabled();
  });

  it("stays held when the pending review it may have finished is simply gone", async () => {
    const { dialog } = await unconfirmed();
    checkAnswer = check({ pending: false, reviews: [] });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check Again" }));
    expect(await within(dialog).findByText(/doesn’t show on GitHub yet/)).toBeInTheDocument();
  });

  it("stays held when the check can't read GitHub", async () => {
    const { dialog } = await unconfirmed();
    checkAnswer = { object: "something else" };
    fireEvent.click(within(dialog).getByRole("button", { name: "Check Again" }));
    expect(await within(dialog).findByText(/couldn’t check GitHub/)).toBeInTheDocument();
  });

  it("settles on the one review like it when the reviews before weren't known", async () => {
    const { dialog } = await unconfirmed({ knownIds: false });
    checkAnswer = check({
      reviews: [newReview({ id: "PRR_old", state: "COMMENTED" }), newReview()],
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check Again" }));
    await waitFor(() => expect(summary()).toHaveValue(""));
    expect(summary()).toBeEnabled();
  });

  it("stays held, and says why, when several reviews look like it", async () => {
    const { dialog } = await unconfirmed({ knownIds: false });
    checkAnswer = check({ reviews: [newReview(), newReview({ id: "PRR_twin" })] });
    fireEvent.click(within(dialog).getByRole("button", { name: "Check Again" }));
    expect(
      await within(dialog).findByText(/more than one review like this one/),
    ).toBeInTheDocument();
    expect(summary()).toHaveValue("Ship it");
    expect(summary()).toBeDisabled();
  });

  it("goes back to an editable draft after Keep as Draft is confirmed, sending nothing", async () => {
    const { dialog, codeHostWrite } = await unconfirmed();
    expect(row("Keep Your Unconfirmed Review as a Draft…")).toBeDefined();
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep as Draft…" }));
    const confirm = await screen.findByRole("dialog", { name: "Keep your review as a draft?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep as Draft" }));
    await waitFor(() => expect(row("Keep Your Unconfirmed Review as a Draft…")).toBeUndefined());
    const reopened = screen.queryByRole("dialog", { name: "Submit review" }) ?? (await openMenu());
    expect(within(reopened).getByRole("textbox")).toHaveValue("Ship it");
    expect(summary()).toBeEnabled();
    expect(within(reopened).getByRole("radio", { name: /Approve/ })).toBeChecked();
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("settles once the check shows the review it made, from ⌘K too", async () => {
    await unconfirmed();
    checkAnswer = check({ reviews: [newReview()] });
    act(() => row("Check Whether Your Review Was Submitted")!.run());
    await waitFor(() => expect(summary()).toHaveValue(""));
    expect(summary()).toBeEnabled();
    expect(row("Check Whether Your Review Was Submitted")).toBeUndefined();
  });
});

describe("reviews per Mac account", () => {
  it("shows another account an empty review, and the first account's summary when it's back", async () => {
    await reviewable();
    show();
    await openMenu();
    fireEvent.change(summary(), { target: { value: "Mine" } });
    act(() => switchMacAccount("other-octo", 2));
    await waitFor(() => expect(summary()).toHaveValue(""));
    act(() => switchMacAccount(VIEWER, 3));
    await waitFor(() => expect(summary()).toHaveValue("Mine"));
  });
});

describe("⌘K's review rows", () => {
  it("opens the menu on the verdict each row names", async () => {
    await reviewable();
    show();
    expect(paletteRows().map((command) => command.label)).toEqual([
      "Submit Review…",
      "Approve Pull Request…",
      "Request Changes…",
    ]);
    act(() => row("Request Changes…")!.run());
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    expect(within(dialog).getByRole("radio", { name: /Request Changes/ })).toBeChecked();
  });

  it("opens on Approve for a request made just before the pull request showed, once", async () => {
    await reviewable();
    // A list row asks before the pull request it opens has rendered its menu.
    requestReviewMenu(PR, "APPROVE");
    const { unmount } = show();
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    expect(within(dialog).getByRole("radio", { name: /Approve/ })).toBeChecked();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    unmount();
    // Seen and closed: showing the pull request again leaves the menu closed.
    show();
    expect(screen.getByRole("button", { name: "Review" })).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens in the header that replaces one that went away with the menu still up", async () => {
    await reviewable();
    requestReviewMenu(PR, "APPROVE");
    const first = show();
    await screen.findByRole("dialog", { name: "Submit review" });
    first.unmount();
    show();
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    expect(within(dialog).getByRole("radio", { name: /Approve/ })).toBeChecked();
  });

  it("reopens in a replacement header long after the request, on the verdict chosen since", async () => {
    await reviewable();
    const start = Date.now();
    const now = vi.spyOn(Date, "now").mockReturnValue(start);
    requestReviewMenu(PR, "APPROVE");
    const first = show();
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    fireEvent.click(within(dialog).getByRole("radio", { name: /Request Changes/ }));
    // The person keeps the menu up past the request's wait before the header is replaced.
    now.mockReturnValue(start + REVIEW_MENU_REQUEST_WAIT_MS + 1_000);
    first.unmount();
    show();
    const reopened = await screen.findByRole("dialog", { name: "Submit review" });
    expect(within(reopened).getByRole("radio", { name: /Request Changes/ })).toBeChecked();
    now.mockRestore();
  });

  it("stays closed in a replacement header once the review it opened for is submitted", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    requestReviewMenu(PR, "APPROVE");
    const first = show();
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit Review" }));
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    first.unmount();
    show();
    expect(screen.getByRole("button", { name: "Review" })).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens only in the view a row aimed at, once that view shows", async () => {
    await reviewable();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (detailActive: boolean) => (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          {[
            { id: "detail", active: detailActive },
            { id: "sidebar", active: true },
          ].map((view) => (
            <PullRequestDestinationContext.Provider key={view.id} value={view}>
              <div data-testid={view.id}>
                <PullRequestReviewMenu state={state()} />
              </div>
            </PullRequestDestinationContext.Provider>
          ))}
        </TooltipProvider>
      </QueryClientProvider>
    );
    const view = render(tree(false));
    act(() => requestReviewMenu(PR, "APPROVE", { destination: "detail", reader: null }));
    // The sidebar shows the same PR, but the request isn't aimed at it.
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(tree(true));
    const dialog = await screen.findByRole("dialog", { name: "Submit review" });
    expect(within(dialog).getByRole("radio", { name: /Approve/ })).toBeChecked();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(
      within(screen.getByTestId("sidebar")).getByRole("button", { name: "Review" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("leaves a request for another pull request to that one", async () => {
    await reviewable();
    requestReviewMenu("https://github.com/acme/app/pull/7", "APPROVE");
    show();
    expect(screen.getByRole("button", { name: "Review" })).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("reviewer pings in Review ▾", () => {
  const REVIEWERS = [
    { login: "alice", kind: "user" as const, state: "PENDING" as const },
    { login: "bob", kind: "user" as const, state: "APPROVED" as const },
  ];
  const ping = vi.fn();
  const pings = { on: true };
  /** Pings each requested reviewer, as a build's integration would, while `pings.on`. */
  function useExamplePings(pr: ReviewerPingPullRequest | null) {
    if (!pr || !pings.on) return new Map<string, ReviewerPingAction>();
    return new Map(
      pr.reviewers
        .filter((reviewer) => reviewer.state === "PENDING")
        .map((reviewer) => [
          reviewer.login.toLowerCase(),
          {
            command: {
              id: `ping:${reviewer.login}`,
              menuLabel: `Ping ${reviewer.login}`,
              paletteLabel: `Ping ${reviewer.login} about #${pr.number}`,
              keywords: ["ping"],
              availability: AVAILABLE,
            },
            run: () => ping(reviewer.login),
          },
        ]),
    );
  }
  withPullRequestIntegrations({
    reviewerPings: {
      sectionLabel: "Ping reviewers",
      useActions: useExamplePings,
      PaletteCommands: () => null,
    },
  });

  function showPr(fields: Partial<PullRequestDetailsState["pr"]> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestReviewMenu state={state({ reviewers: REVIEWERS, ...fields })} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    ping.mockReset();
    pings.on = true;
  });

  it("lists the requested reviewers' pings, and a pick closes the menu and pings", async () => {
    await reviewable();
    showPr();
    const menu = await openMenu();
    const section = await within(menu).findByRole("region", { name: "Ping reviewers" });
    expect(within(section).queryByRole("button", { name: /bob/ })).toBeNull();
    fireEvent.click(within(section).getByRole("button", { name: "Ping alice" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Submit review" })).toBeNull());
    expect(ping).toHaveBeenCalledWith("alice");
  });

  it("offers pings where this Mac can't submit reviews, beside the way to GitHub", async () => {
    // GitHub reads on this Mac, but no review writes: reviewing is a link to GitHub.
    await readyShell({ version: 1, operations: [...GITHUB_WRITE_SET_V1] });
    readAs(VIEWER);
    showPr();
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    const menu = await screen.findByRole("dialog", { name: "Review" });
    expect(within(menu).getByRole("button", { name: /Review on GitHub/ })).toBeTruthy();
    const section = within(menu).getByRole("region", { name: "Ping reviewers" });
    fireEvent.click(within(section).getByRole("button", { name: "Ping alice" }));
    expect(ping).toHaveBeenCalledWith("alice");
  });

  it("offers pings on a pull request this Mac never reviews", async () => {
    installShell({ getDesktopFeatures: async () => ({}) });
    showPr({ url: "https://3.spoke.example.com/example-org/app/pull/42" });
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    const menu = await screen.findByRole("dialog", { name: "Review" });
    fireEvent.click(within(menu).getByRole("button", { name: "Ping alice" }));
    expect(ping).toHaveBeenCalledWith("alice");
  });

  it("keeps the header without a Review button where there's nothing to review or ping", async () => {
    pings.on = false;
    installShell({ getDesktopFeatures: async () => ({}) });
    showPr({ url: "https://3.spoke.example.com/example-org/app/pull/42" });
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
  });

  it("shows no ping section where no reviewer can be pinged", async () => {
    pings.on = false;
    await reviewable();
    showPr();
    const menu = await openMenu();
    expect(within(menu).queryByRole("region", { name: "Ping reviewers" })).toBeNull();
  });
});

describe("one-click Approve, as a pull request's row and ⌘K offer it", () => {
  const key = () => reviewSubmitKey("user-1", PR, VIEWER);
  const openReview = vi.fn();
  // The row's list, as it reads the PR now; a test changes what it has.
  const listed: { current: ApprovalSource } = {
    current: { reader: null, facts: () => ({ open: true, author: "amy" }) },
  };
  let unpublish = () => {};
  beforeEach(() => {
    listed.current = { reader: null, facts: () => ({ open: true, author: "amy" }) };
    unpublish = APPROVAL_SOURCES.publish("row-list", listed);
  });
  afterEach(() => unpublish());
  const approve = (overrides: Partial<ApproveNowOptions> = {}) =>
    approveNow({
      owner: "user-1",
      prUrl: PR,
      headSha: HEAD,
      origin: null,
      queryClient: new QueryClient(),
      conversationId: "conv_1",
      readAs: null,
      openReview,
      source: "row-list",
      ...overrides,
    });
  const sentReviews = (codeHostWrite: ReturnType<typeof vi.fn>) =>
    codeHostWrite.mock.calls.map(
      ([request]) => (request as { params: Record<string, unknown> }).params,
    );
  const refusal = (code: string) => ({ ok: false, error: { code, message: "GitHub said no." } });
  /** The last error toast's Retry, as Sonner would run it on a click. */
  const toastRetry = () => {
    const options = vi.mocked(toast.error).mock.calls.at(-1)?.[1];
    return (options?.action as unknown as { onClick: () => void } | undefined)?.onClick;
  };
  /** A first send GitHub refuses, then a shell that would approve any resend. */
  async function refusedOnce() {
    const codeHostWrite = vi
      .fn()
      .mockResolvedValueOnce(refusal("rejected"))
      .mockResolvedValue(submitted({ body: "" }));
    await reviewable(codeHostWrite);
    approve();
    await waitFor(() => expect(toastRetry()).toBeDefined());
    return codeHostWrite;
  }
  const switchWorkspaceUser = () => {
    currentUser.id = "user-2";
    act(() => {
      for (const listener of [...identityListeners]) listener();
    });
  };

  beforeEach(() => {
    vi.mocked(toast.loading).mockClear();
    vi.mocked(toast.error).mockClear();
    vi.mocked(toast.warning).mockClear();
    vi.mocked(toast.dismiss).mockClear();
    openReview.mockClear();
  });

  it("approves at once with no comment, pinned to the head, with progress then the result", async () => {
    const codeHostWrite = vi.fn(async () => submitted({ body: "" }));
    await reviewable(codeHostWrite);
    expect(approve()).toBe("sent");
    expect(toast.loading).toHaveBeenCalledWith("Approving acme/app#42…", expect.anything());
    await waitFor(() => expect(toast.dismiss).toHaveBeenCalledWith("progress"));
    expect(sentReviews(codeHostWrite)).toEqual([
      { repository: "acme/app", number: 42, event: "APPROVE", body: "", expected_head: HEAD },
    ]);
    expect(readReviewSubmit(key()).status).toEqual({ kind: "idle" });
  });

  it("leaves a draft summary, a review in flight, an unknown head, or reviews off to the menu", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    updateReviewSubmit(key(), (stored) => ({ ...stored, body: "Needs one more test" }));
    expect(approve()).toBe("open-menu");
    // The draft stays for the menu to show, unsent.
    expect(readReviewSubmit(key()).body).toBe("Needs one more test");
    updateReviewSubmit(key(), (stored) => ({
      ...stored,
      body: "",
      status: { kind: "submitting" },
    }));
    expect(approve()).toBe("open-menu");
    updateReviewSubmit(key(), (stored) => ({ ...stored, status: { kind: "idle" } }));
    expect(approve({ headSha: "" })).toBe("open-menu");
    setReviewsFlagForTests(false);
    expect(approve()).toBe("open-menu");
    expect(codeHostWrite).not.toHaveBeenCalled();
    expect(toast.loading).not.toHaveBeenCalled();
  });

  it("leaves a PR the list now has as closed, your own, or unread to the menu", async () => {
    const codeHostWrite = vi.fn(async () => submitted());
    await reviewable(codeHostWrite);
    const listing = (facts: ApprovalSource["facts"]) => {
      listed.current = { reader: null, facts };
    };
    listing(() => ({ open: false, author: "amy" }));
    expect(approve()).toBe("open-menu");
    listing(() => ({ open: true, author: VIEWER.toUpperCase() }));
    expect(approve()).toBe("open-menu");
    listing(() => null);
    expect(approve()).toBe("open-menu");
    // The list went, or reads as the page's own login now.
    listing(() => ({ open: true, author: "amy" }));
    expect(approve({ source: "gone" })).toBe("open-menu");
    listed.current = { ...listed.current, reader: { host: "github.com", login: VIEWER } };
    expect(approve()).toBe("open-menu");
    expect(codeHostWrite).not.toHaveBeenCalled();
  });

  it("says a refusal with Retry, which approves again, off the Pull Requests page", async () => {
    const codeHostWrite = await refusedOnce();
    expect(vi.mocked(toast.error).mock.calls.at(-1)).toEqual([
      "Couldn’t approve acme/app#42: GitHub said no.",
      expect.objectContaining({
        id: "progress",
        action: expect.objectContaining({ label: "Retry" }),
      }),
    ]);
    expect(readReviewSubmit(key()).status).toMatchObject({ kind: "refused", code: "rejected" });
    toastRetry()!();
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(readReviewSubmit(key()).status).toEqual({ kind: "idle" }));
  });

  it("offers no Retry where a resend would only be refused again", async () => {
    const codeHostWrite = vi.fn(async () => refusal("head_changed"));
    await reviewable(codeHostWrite);
    approve();
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toastRetry()).toBeUndefined();
    expect(approveRetryFor(key())).toBeUndefined();
  });

  it("opens the Review menu, keeping the draft, when Retry can no longer send", async () => {
    const codeHostWrite = await refusedOnce();
    updateReviewSubmit(key(), (stored) => ({ ...stored, body: "Actually, one comment" }));
    toastRetry()!();
    expect(openReview).toHaveBeenCalledTimes(1);
    expect(readReviewSubmit(key()).body).toBe("Actually, one comment");
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("opens the Review menu instead when reviews from this Mac were turned off", async () => {
    const codeHostWrite = await refusedOnce();
    setReviewsFlagForTests(false);
    toastRetry()!();
    expect(openReview).toHaveBeenCalledTimes(1);
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("sends nothing from Retry once the Mac account changed", async () => {
    const codeHostWrite = await refusedOnce();
    switchMacAccount("other-octo", 2);
    toastRetry()!();
    expect(openReview).toHaveBeenCalledTimes(1);
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("retires its toast, and sends nothing from Retry, once the workspace user changed", async () => {
    const codeHostWrite = await refusedOnce();
    const retry = toastRetry()!;
    switchWorkspaceUser();
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
    retry();
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(codeHostWrite).toHaveBeenCalledTimes(1);
    expect(openReview).not.toHaveBeenCalled();
  });

  it("retires its progress toast when the workspace user changes while it's sent", async () => {
    const codeHostWrite = vi.fn(() => new Promise<WriteAnswer>(() => {}));
    await reviewable(codeHostWrite);
    approve();
    await waitFor(() => expect(codeHostWrite).toHaveBeenCalledTimes(1));
    switchWorkspaceUser();
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
    expect(toast.warning).not.toHaveBeenCalled();
    expect(readReviewSubmit(key()).status).toMatchObject({ kind: "uncertain" });
  });

  it("says GitHub didn't confirm it, with a way to open the review and check", async () => {
    const codeHostWrite = vi.fn(async () => ({
      ok: false,
      error: { code: "outcome_unknown", message: "GitHub didn't confirm your review." },
      known_review_ids: [],
    }));
    await reviewable(codeHostWrite);
    approve();
    await waitFor(() => expect(toast.warning).toHaveBeenCalled());
    const [message, options] = vi.mocked(toast.warning).mock.calls.at(-1)!;
    expect(message).toBe(
      "GitHub didn’t confirm approving acme/app#42. Open its review to check whether it went through.",
    );
    expect(options).toMatchObject({ id: "progress", action: { label: "Open Review" } });
    const watching = identityListeners.size;
    (options!.action as unknown as { onClick: () => void }).onClick();
    expect(openReview).toHaveBeenCalledTimes(1);
    expect(readReviewSubmit(key()).status).toMatchObject({ kind: "uncertain" });
    // The clicked toast is gone, and stops watching for a workspace user change.
    expect(identityListeners.size).toBe(watching - 1);
  });

  it("on the Pull Requests page, shows no toast and leaves Retry to its notice", async () => {
    const codeHostWrite = vi
      .fn()
      .mockResolvedValueOnce(refusal("rejected"))
      .mockResolvedValue(submitted({ body: "" }));
    await reviewable(codeHostWrite);
    const succeeded = vi.fn();
    const origin = { begin: vi.fn(() => ({ succeeded })) };
    approve({ origin });
    expect(toast.loading).not.toHaveBeenCalled();
    await waitFor(() => expect(origin.begin).toHaveBeenCalled());
    expect(origin.begin).toHaveBeenCalledWith(
      PR,
      { login: VIEWER, generation: 1 },
      { headSha: HEAD },
    );
    await waitFor(() => expect(approveRetryFor(key())).toBeDefined());
    expect(toast.error).not.toHaveBeenCalled();
    approveRetryFor(key())!();
    await waitFor(() => expect(succeeded).toHaveBeenCalledWith("approved"));
    expect(approveRetryFor(key())).toBeUndefined();
  });
});
