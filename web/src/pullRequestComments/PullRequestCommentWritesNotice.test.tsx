// The comment box when nothing can post, with the real read-path and write-path stores over a
// stand-in shell: it stays editable, says why, offers what works on this shell, and enables
// itself once that's fixed, with the same draft, which then posts once.

import type * as IdentityModule from "@/lib/identity";
import type * as CodeHostSignInModule from "@/lib/codeHostSignIn";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, signIn, hostFetch } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  signIn: vi.fn(),
  hostFetch: vi.fn(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: () => () => {},
  // The session's machine: a post that reached it would land here.
  authenticatedFetch: hostFetch,
}));
// The dialog is the sign-in controller's own; here it answers as the user would finish it.
vi.mock("@/lib/codeHostSignIn", async (importOriginal) => ({
  ...(await importOriginal<typeof CodeHostSignInModule>()),
  signInToGitHubOnThisMac: signIn,
}));

import {
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  readOnSessionMachine,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  PANEL_READ_OPERATIONS,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { useMacWriteRoute } from "@/newPullRequestPage/hooks/usePullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { PullRequestCommentComposer } from "@/newPullRequestPage/shell/PullRequestCommentComposer";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const PR = "https://ghe.example.com/acme/app/pull/42";
const HOST = "ghe.example.com";

const posted = (body: string) => ({
  ok: true,
  data: {
    object: "session.github.pr_comment",
    comment: { author: "mac-octo", body, created_at: "2026-11-05T10:00:00Z", url: `${PR}#c-1` },
  },
  actor: { login: "mac-octo" },
});

interface ShellOptions {
  /** The shell can sign in to GitHub itself; an older one can't. */
  signInBridge?: boolean;
  writes?: unknown;
}

/** A shell reading and writing `HOST` as mac-octo; `answers` replies to each write in turn. */
async function installShell(answers: unknown[], options: ShellOptions = {}) {
  const { signInBridge = true, writes = { version: 1, operations: [...GITHUB_WRITE_SET_V1] } } =
    options;
  let generation = 1;
  const reports: unknown[] = [];
  const write = vi.fn(async (_request: unknown) =>
    answers.length > 1 ? answers.shift() : answers[0],
  );
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [{ login: "mac-octo" }] })),
    codeHostWrite: write,
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: {
          intent: { mode: "automatic" },
          effective: { host: HOST, login: "mac-octo", generation },
          accounts: [{ login: "mac-octo", active: true, state: "success" }],
        },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    ...(signInBridge
      ? {
          codeHostSignIn: {
            accounts: vi.fn(),
            start: vi.fn(),
            openVerification: vi.fn(),
            cancel: vi.fn(),
            onProgress: () => () => {},
            onAccountsChanged: () => () => {},
          },
        }
      : {}),
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          sign_in: signInBridge,
          writes,
        },
      ],
    }),
    reportPullRequestCommentWrites: (report: unknown) => reports.push(report),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  expect(
    acceptMacAnswer(macReadTicket("github", HOST), {
      identity: { host: HOST, login: "mac-octo", generation: 1 },
    }),
  ).toBe("accepted");
  setMacWritesReleaseFlagForTests(true);
  return { write, reports, nextGeneration: () => (generation += 1) };
}

/** The box as the panel shows it: blocked whenever this Mac can't post and the host can't. */
function Box() {
  const route = useMacWriteRoute(PR);
  return (
    <PullRequestCommentComposer
      conversationId="session-1"
      prUrl={PR}
      account={route ? `@${route.actor.login}` : null}
      writesBlocked={!route}
    />
  );
}

function renderBox() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <Box />
    </QueryClientProvider>,
  );
}

const box = () => screen.getByRole("textbox", { name: "New PR comment" });
const postButton = () => screen.getByRole("button", { name: "Post comment" });
const notice = () => screen.queryByTestId("pull-request-comment-writes-notice");

async function type(body: string) {
  fireEvent.change(box(), { target: { value: body } });
  await waitFor(() => expect(box()).toHaveValue(body));
}

beforeEach(() => {
  currentUser.id = "user-1";
  signIn.mockReset();
  hostFetch.mockReset();
  resetDesktopPullRequestWritesForTests();
});

afterEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  writeMacWritesIntent("automatic");
});

describe("a post this Mac refused because its sign-in expired", () => {
  it("signs in to the PR's host, keeps the draft, and posts it once", async () => {
    const shell = await installShell([
      {
        ok: false,
        error: {
          code: "account_invalid",
          message: "The GitHub sign-in on this Mac has expired. Nothing changed on GitHub.",
        },
      },
      posted("Ship it"),
    ]);
    signIn.mockImplementation(async () => {
      shell.nextGeneration();
      return { ok: true, login: "mac-octo" };
    });
    renderBox();
    await type("Ship it");
    fireEvent.click(postButton());

    await waitFor(() => expect(notice()).toHaveTextContent(`Sign in to ${HOST} again`));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The GitHub sign-in on this Mac has expired.",
    );
    expect(notice()).toHaveTextContent(`gh auth login --hostname ${HOST}`);
    expect(box()).toHaveValue("Ship it");
    // A refusal doesn't block posting again; only the sign-in is offered beside it.
    expect(postButton()).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: `Sign in to ${HOST}…` }));
    await waitFor(() => expect(notice()).toBeNull());
    expect(signIn).toHaveBeenCalledExactlyOnceWith({ host: HOST });
    expect(box()).toHaveValue("Ship it");
    expect(screen.queryByRole("alert")).toBeNull();

    // The host's account was read again, and the same draft posts once as it.
    await waitFor(() => expect(postButton()).toBeEnabled());
    fireEvent.click(postButton());
    await waitFor(() => expect(box()).toHaveValue(""));
    expect(shell.write).toHaveBeenCalledTimes(2);
    expect(shell.write.mock.calls[1]![0]).toMatchObject({
      host: HOST,
      operation: "pull_request_comment",
      params: { body: "Ship it" },
      actor: { login: "mac-octo" },
    });
    expect(shell.reports).toEqual([
      { reason: "account_invalid", outcome: "shown" },
      { reason: "account_invalid", outcome: "sign_in_started" },
      { reason: "account_invalid", outcome: "sign_in_succeeded" },
      { reason: "account_invalid", outcome: "resolved" },
    ]);
  });

  it("keeps the refusal and the draft when the sign-in is cancelled or fails", async () => {
    const shell = await installShell([
      { ok: false, error: { code: "signed_out", message: "No GitHub account is signed in." } },
    ]);
    signIn.mockResolvedValue({ ok: false, code: "cancelled" });
    renderBox();
    await type("Still here");
    fireEvent.click(postButton());
    await waitFor(() => expect(notice()).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: `Sign in to ${HOST}…` }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: `Sign in to ${HOST}…` })).toBeEnabled(),
    );
    expect(notice()).not.toBeNull();
    expect(box()).toHaveValue("Still here");
    expect(shell.reports).toContainEqual({ reason: "signed_out", outcome: "sign_in_failed" });
    expect(shell.reports).not.toContainEqual({ reason: "signed_out", outcome: "resolved" });
  });

  it("offers only the Terminal steps on a shell that can't sign in itself", async () => {
    await installShell(
      [{ ok: false, error: { code: "signed_out", message: "No GitHub account is signed in." } }],
      { signInBridge: false },
    );
    renderBox();
    await type("From an older shell");
    fireEvent.click(postButton());
    await waitFor(() => expect(notice()).toHaveTextContent(`gh auth login --hostname ${HOST}`));
    expect(screen.queryByRole("button", { name: /^Sign in to/ })).toBeNull();
    expect(notice()).toHaveTextContent("In Terminal, run");
  });
});

describe("a refused sign-in, then something else stops posting", () => {
  it.each([
    ["the writes flag turns off", () => setMacWritesReleaseFlagForTests(false), "isn’t turned on"],
    ["Settings turn Off", () => writeMacWritesIntent("off"), "are turned off"],
    [
      "the PR moves to the session's machine",
      () => readOnSessionMachine(PR),
      "The session’s machine can’t post comments",
    ],
  ])(
    "when %s, the current reason blocks Post and nothing is sent",
    async (_label, change, copy) => {
      const shell = await installShell([
        { ok: false, error: { code: "account_invalid", message: "The sign-in has expired." } },
      ]);
      renderBox();
      await type("Keep me");
      fireEvent.click(postButton());
      await waitFor(() => expect(notice()).toHaveTextContent(`Sign in to ${HOST} again`));
      expect(shell.write).toHaveBeenCalledOnce();

      act(change);
      await waitFor(() => expect(notice()).toHaveTextContent(copy));
      expect(notice()).not.toHaveTextContent("again, then post your draft");
      expect(postButton()).toBeDisabled();
      // The refusal stays beside the draft as what happened last.
      expect(screen.getByRole("alert")).toHaveTextContent("The sign-in has expired.");
      // Enter in the box or a stray submit sends nothing anywhere.
      fireEvent.submit(box().closest("form")!);
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
      expect(shell.write).toHaveBeenCalledOnce();
      expect(hostFetch).not.toHaveBeenCalled();
      expect(box()).toHaveValue("Keep me");
    },
  );
});

describe("a PR sent back to the session's machine, which can't post", () => {
  it("offers Read on this Mac for a GitHub PR this Mac could post to", async () => {
    await installShell([posted("x")]);
    readOnSessionMachine(PR);
    renderBox();
    await waitFor(() => expect(notice()).toHaveTextContent("Read it on this Mac to comment"));
    expect(screen.getByRole("button", { name: "Read on this Mac" })).toBeEnabled();
  });

  it("never promises commenting from this Mac on a GitSpoke PR", async () => {
    const spoke = "https://2.spoke.example.com/example-org/universe/pull/7";
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }],
      }),
    };
    await desktopReadTarget(spoke);
    setMacWritesReleaseFlagForTests(true);
    readOnSessionMachine(spoke);
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <PullRequestCommentComposer
          conversationId="session-1"
          prUrl={spoke}
          account={null}
          writesBlocked
          provider={gitProviderCopy("gitspoke")}
        />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(notice()).toHaveTextContent(
        "The session’s machine can’t post comments on this pull request.",
      ),
    );
    expect(notice()).not.toHaveTextContent("this Mac");
    expect(screen.queryByRole("button", { name: "Read on this Mac" })).toBeNull();
    expect(screen.getByRole("link", { name: "Open on GitSpoke" })).toBeVisible();
  });
});

describe("a box this Mac can't post from yet", () => {
  it("stays editable while the flag turns off, explains it, and enables again with the draft", async () => {
    const shell = await installShell([posted("Typed while off")]);
    renderBox();
    expect(notice()).toBeNull();
    act(() => setMacWritesReleaseFlagForTests(false));
    await waitFor(() =>
      expect(notice()).toHaveTextContent("isn’t turned on in this version of the app"),
    );
    await type("Typed while off");
    expect(postButton()).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", PR);
    act(() => setMacWritesReleaseFlagForTests(true));
    await waitFor(() => expect(notice()).toBeNull());
    expect(box()).toHaveValue("Typed while off");
    fireEvent.click(postButton());
    await waitFor(() => expect(box()).toHaveValue(""));
    expect(shell.write).toHaveBeenCalledOnce();
    expect(shell.reports).toEqual([
      { reason: "release_off", outcome: "shown" },
      { reason: "release_off", outcome: "resolved" },
    ]);
  });

  it("turns Settings' Off back to Automatic from the box itself", async () => {
    await installShell([posted("x")]);
    writeMacWritesIntent("off");
    renderBox();
    await waitFor(() =>
      expect(notice()).toHaveTextContent("Changes as this Mac’s account are turned off"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Make changes as this Mac’s account" }));
    await waitFor(() => expect(notice()).toBeNull());
    expect(window.localStorage.getItem("omnigent:desktop-code-host-writes")).toBeNull();
  });

  it("checks a shell that doesn't offer writes again, and offers no sign-in there", async () => {
    await installShell([posted("x")], { writes: null });
    renderBox();
    await waitFor(() =>
      expect(notice()).toHaveTextContent("This Mac isn’t posting comments right now"),
    );
    expect(screen.queryByRole("button", { name: /^Sign in to/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
  });
});
