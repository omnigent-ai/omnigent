// PullRequestMerge against the real merge hooks, with fetch stubbed: the button
// follows mergeStateStatus and auto-merge, names the repository's default method,
// and a merge posts once, pinned to the head the dialog opened on.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  PullRequest,
  PullRequestMergeMethod,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { NativeStackMergeHandoff } from "@/pullRequestPage/nativeStackMerge";
import type * as PullRequestPageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import { resetPullRequestStateHoldsForTests } from "@/lib/pullRequestStateHolds";
import {
  mergeButtonState,
  PullRequestMerge,
  type MergeAccounts,
  type MergeReadyAction,
} from "./PullRequestMerge";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);
// The merge bar shows only where the host serves merging, so its merge options read is served.
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => {
  const original = await importOriginal<typeof PullRequestPageRouteModule>();
  return {
    ...original,
    usePullRequestMethodServed: (...args: Parameters<typeof original.usePullRequestMethodServed>) =>
      args[1] === "getMergeOptions" || original.usePullRequestMethodServed(...args),
  };
});

const PR_URL = "https://github.com/acme/app/pull/7";
const MERGE_PATH = "/v1/sessions/conv_1/resources/github/merge";
const MERGE_AS_PATH = "/v1/sessions/conv_1/resources/github/merge-as";
const DISABLE_PATH = "/v1/sessions/conv_1/resources/github/disable-auto-merge";
const OPTIONS_PATH = "/v1/sessions/conv_1/resources/github/merge-options";
const PREFERENCES_PATH = "/v1/sessions/conv_1/resources/github/preferences";
const HEAD = "a".repeat(40);
const AUTO_MERGE = { merge_method: "MERGE", enabled_by: "dev", enabled_at: "2026-10-21T06:02:55Z" };

afterEach(() => {
  vi.unstubAllGlobals();
  // A merge a test left unanswered holds the PR's state changes; the next test starts clean.
  resetPullRequestStateHoldsForTests();
});

function pullRequest(overrides: Partial<PullRequest> = {}): PullRequest {
  return {
    number: 7,
    title: "Ship the widget",
    state: "OPEN",
    url: PR_URL,
    is_draft: false,
    author: "dev",
    base_ref: "main",
    head_ref: "widget",
    head_sha: HEAD,
    checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    merge_state_status: "CLEAN",
    auto_merge: null,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function mergeOptions(methods: PullRequestMergeMethod[], fallback: PullRequestMergeMethod | null) {
  return () => json({ object: "session.github.merge_options", methods, default: fallback });
}

const allMethods = mergeOptions(["merge", "squash", "rebase"], "squash");

interface Post {
  path: string;
  body: Record<string, unknown>;
  respond: (response: Response) => void;
}

/** Stub fetch: posts wait for the test to answer; merge options answer `options`. */
function stubFetch(options: () => Response) {
  const posts: Post[] = [];
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === OPTIONS_PATH) return Promise.resolve(options());
    if (init?.method === "POST") {
      return new Promise<Response>((respond) => {
        posts.push({ path: url.pathname, body: JSON.parse(String(init.body)), respond });
      });
    }
    return Promise.resolve(new Response("{}", { status: 404 }));
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, posts };
}

/** A page's ready caption, naming what the button would do. */
const readyCaption: Record<MergeReadyAction, string> = {
  merge: "Ready caption: merge",
  queue: "Ready caption: queue",
};

function renderMerge(
  pr: PullRequest,
  {
    caption = false,
    handoff = null,
    stackPending = false,
    accounts,
  }: {
    caption?: boolean;
    handoff?: NativeStackMergeHandoff | null;
    stackPending?: boolean;
    accounts?: MergeAccounts;
  } = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  interface Stack {
    handoff?: NativeStackMergeHandoff | null;
    stackPending?: boolean;
  }
  const tree = (next: PullRequest, stack: Stack = { handoff, stackPending }) => (
    <QueryClientProvider client={client}>
      <PullRequestMerge
        conversationId="conv_1"
        prUrl={PR_URL}
        pr={next}
        readyCaption={caption ? readyCaption : undefined}
        handoff={stack.handoff ?? null}
        stackPending={stack.stackPending ?? false}
        accounts={accounts}
      />
    </QueryClientProvider>
  );
  const view = render(tree(pr));
  return {
    client,
    invalidate,
    rerender: (next: PullRequest, stack?: Stack) => view.rerender(tree(next, stack)),
  };
}

async function openDialog(buttonName = "Squash and merge") {
  fireEvent.click(await screen.findByRole("button", { name: buttonName }));
  expect(await screen.findByText("Merge pull request #7?")).toBeInTheDocument();
}

describe("mergeButtonState", () => {
  it.each([
    ["CLEAN", "ready", undefined],
    ["UNSTABLE", "ready", "Checks that aren’t required are failing or still running."],
    [
      "HAS_HOOKS",
      "ready",
      "The repository’s pre-receive hooks run on merge and can still reject it.",
    ],
    ["BLOCKED", "blocked", "Required checks or reviews haven’t passed."],
    ["BEHIND", "blocked", "Update the branch first."],
    ["DIRTY", "blocked", "Resolve conflicts first."],
    ["DRAFT", "blocked", "Mark the pull request ready for review first."],
    ["OTHER", "blocked", "GitHub reports that it can’t merge yet."],
    ["UNKNOWN", "checking", "Checking whether it can merge…"],
    [null, "checking", "Checking whether it can merge…"],
  ])("%s → %s", (status, kind, hint) => {
    const state = mergeButtonState(pullRequest({ merge_state_status: status }));
    expect(state.kind).toBe(kind);
    expect("hint" in state ? state.hint : undefined).toBe(hint);
  });

  it("blocks a draft that GitHub reports as clean", () => {
    // GitHub reports a draft's merge state as CLEAN; only its draft flag says it can't merge.
    expect(mergeButtonState(pullRequest({ merge_state_status: "CLEAN", is_draft: true }))).toEqual({
      kind: "blocked",
      hint: "Mark the pull request ready for review first.",
    });
  });

  it("waits for a head commit before allowing a merge", () => {
    expect(mergeButtonState(pullRequest({ head_sha: undefined })).kind).toBe("checking");
  });

  it("puts auto-merge ahead of a clean state", () => {
    expect(mergeButtonState(pullRequest({ auto_merge: AUTO_MERGE }))).toEqual({
      kind: "auto_merge",
    });
  });
});

it.each([
  ["squash", "Squash and merge"],
  ["rebase", "Rebase and merge"],
  ["merge", "Merge pull request"],
] as const)("names the button after the default %s method", async (method, label) => {
  stubFetch(mergeOptions(["merge", "squash", "rebase"], method));
  renderMerge(pullRequest());

  expect(await screen.findByRole("button", { name: label })).toBeEnabled();
});

it("disables the button unless the PR is clean", async () => {
  stubFetch(allMethods);
  renderMerge(pullRequest({ merge_state_status: "BLOCKED" }));

  expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeDisabled();
  expect(screen.getByText("Required checks or reviews haven’t passed.")).toBeInTheDocument();
});

describe("with auto-merge enabled", () => {
  it("offers to disable it without who enabled it", () => {
    const { fetch } = stubFetch(allMethods);
    renderMerge(pullRequest({ auto_merge: AUTO_MERGE }));

    expect(screen.getByRole("button", { name: "Disable auto-merge" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "Merge" })).toHaveTextContent(
      "This pull request will merge automatically when all requirements are met.",
    );
    expect(screen.queryByText(/dev/)).toBeNull();
    // Merge options name the merge button, which auto-merge replaces.
    expect(fetch).not.toHaveBeenCalled();
  });

  it("disables it once and stays busy until the refetch drops it", async () => {
    const { posts } = stubFetch(allMethods);
    const { invalidate, rerender } = renderMerge(pullRequest({ auto_merge: AUTO_MERGE }));

    const button = screen.getByRole("button", { name: "Disable auto-merge" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ path: DISABLE_PATH, body: { pr_url: PR_URL } });
    await act(async () => {
      posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["pr-page-github-info", "conv_1"] });
    expect(screen.getByRole("button", { name: "Disable auto-merge" })).toBeDisabled();
    rerender(pullRequest());
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeEnabled();
    expect(posts).toHaveLength(1);
  });

  it("shows GitHub's refusal inline", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest({ auto_merge: AUTO_MERGE }));

    fireEvent.click(screen.getByRole("button", { name: "Disable auto-merge" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    const detail = "The GitHub account on the host cannot disable auto-merge.";
    await act(async () => {
      posts[0].respond(json({ detail }, 400));
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("button", { name: "Disable auto-merge" })).toBeEnabled();
  });
});

it("merges once at the confirmed head", async () => {
  const { posts } = stubFetch(allMethods);
  const { invalidate, rerender } = renderMerge(pullRequest());

  await openDialog();
  const picker = await screen.findByRole("combobox", { name: "Merge method" });
  expect(picker).toHaveTextContent("Squash and merge");
  fireEvent.click(picker);
  fireEvent.click(screen.getByRole("option", { name: "Rebase and merge" }));
  // A poll that moves the head after the dialog opened doesn't change what merges.
  rerender(pullRequest({ head_sha: "b".repeat(40) }));

  const dialog = screen.getByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: "Rebase and merge" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toMatchObject({
    path: MERGE_PATH,
    body: { pr_url: PR_URL, merge_method: "rebase", expected_head: HEAD },
  });
  expect(screen.getByRole("button", { name: "Merging…" })).toBeDisabled();
  // Nothing claims the merge happened before GitHub answers.
  expect(screen.queryByText("Merge requested. Refreshing…")).not.toBeInTheDocument();

  await act(async () => {
    posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
  });

  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["pr-page-github-info", "conv_1"] });
  expect(posts).toHaveLength(1);
});

describe("a page's ready caption", () => {
  it("shows beside an enabled merge button", async () => {
    stubFetch(allMethods);
    renderMerge(pullRequest(), { caption: true });
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeEnabled();
    expect(screen.getByText("Ready caption: merge")).toBeInTheDocument();
  });

  it.each([
    ["blocked", { merge_state_status: "BLOCKED" }],
    ["still computing", { merge_state_status: "UNKNOWN" }],
    ["auto-merging", { auto_merge: AUTO_MERGE }],
  ] as const)("stays away while the PR is %s", async (_case, overrides) => {
    stubFetch(allMethods);
    renderMerge(pullRequest(overrides), { caption: true });
    await screen.findByRole("region", { name: "Merge" });
    expect(screen.queryByText(/Ready caption/)).toBeNull();
  });

  it("goes while an accepted merge waits for the refetch, though the PR still reads clean", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), { caption: true });
    await openDialog();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Squash and merge" }),
    );
    await waitFor(() => expect(posts).toHaveLength(1));
    await act(async () => {
      posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
    });
    expect(await screen.findByText("Merge requested. Refreshing…")).toBeInTheDocument();
    expect(screen.queryByText(/Ready caption/)).toBeNull();
  });

  it("is never shown by the classic panel, which passes none", async () => {
    stubFetch(allMethods);
    renderMerge(pullRequest());
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeEnabled();
    expect(screen.queryByText(/Ready caption/)).toBeNull();
  });
});

it("keeps the button busy after a merge until the refetch shows the new state", async () => {
  const { posts } = stubFetch(allMethods);
  const { rerender } = renderMerge(pullRequest());

  await openDialog();
  fireEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "Squash and merge" }),
  );
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => {
    posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
  });

  expect(await screen.findByText("Merge requested. Refreshing…")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Squash and merge" })).toBeDisabled();
  // A new head is a new merge decision.
  rerender(pullRequest({ head_sha: "c".repeat(40) }));
  expect(screen.getByRole("button", { name: "Squash and merge" })).toBeEnabled();
});

it("shows GitHub's merge refusal inline and keeps the dialog open", async () => {
  const { posts } = stubFetch(allMethods);
  renderMerge(pullRequest());

  await openDialog();
  const confirm = within(screen.getByRole("dialog")).getByRole("button", {
    name: "Squash and merge",
  });
  fireEvent.click(confirm);
  await waitFor(() => expect(posts).toHaveLength(1));
  const detail = "The pull request's branch changed since the panel loaded.";
  await act(async () => {
    posts[0].respond(json({ detail }, 400));
  });

  expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(confirm).toBeEnabled();
});

it.each([
  ["UNSTABLE", "Checks that aren’t required are failing or still running."],
  ["HAS_HOOKS", "The repository’s pre-receive hooks run on merge and can still reject it."],
])(
  "lets a %s PR merge, with the caveat beside the button and in the dialog",
  async (status, hint) => {
    stubFetch(allMethods);
    renderMerge(pullRequest({ merge_state_status: status }));

    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeEnabled();
    expect(screen.getByText(hint)).toBeInTheDocument();
    await openDialog();
    expect(within(screen.getByRole("dialog")).getByRole("note")).toHaveTextContent(hint);
  },
);

it("says why the dialog can't merge when merge options fail to load", async () => {
  stubFetch(() => json({ detail: "GitHub could not read this repository's merge settings." }, 400));
  renderMerge(pullRequest());

  await openDialog("Merge pull request");

  expect(await screen.findByRole("alert")).toHaveTextContent("could not read this repository");
  expect(
    within(screen.getByRole("dialog")).getByRole("button", { name: "Merge pull request" }),
  ).toBeDisabled();
});

describe("a change the refetch hasn't shown", () => {
  afterEach(() => vi.useRealTimers());

  it("stops holding the merge button busy after 30 seconds", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest());
    await openDialog();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Squash and merge" }),
    );
    await waitFor(() => expect(posts).toHaveLength(1));
    vi.useFakeTimers();
    await act(async () => {
      posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
    });
    expect(screen.getByText("Merge requested. Refreshing…")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(30_000));

    expect(screen.queryByText("Merge requested. Refreshing…")).toBeNull();
    expect(screen.getByRole("button", { name: "Squash and merge" })).toBeEnabled();
  });

  it("stops holding disable auto-merge busy after 30 seconds", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest({ auto_merge: AUTO_MERGE }));
    fireEvent.click(screen.getByRole("button", { name: "Disable auto-merge" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    vi.useFakeTimers();
    await act(async () => {
      posts[0].respond(json({ object: "session.github.pr_merge", pr_url: PR_URL }));
    });
    expect(screen.getByRole("button", { name: "Disable auto-merge" })).toBeDisabled();

    act(() => vi.advanceTimersByTime(30_000));

    expect(screen.getByRole("button", { name: "Disable auto-merge" })).toBeEnabled();
  });
});

it("can't confirm when the repository allows no method", async () => {
  stubFetch(mergeOptions([], null));
  renderMerge(pullRequest());

  await openDialog("Merge pull request");

  expect(await screen.findByRole("alert")).toHaveTextContent("allows no merge method");
  expect(
    within(screen.getByRole("dialog")).getByRole("button", { name: "Merge pull request" }),
  ).toBeDisabled();
});

describe("a PR in GitHub's own stack", () => {
  it("hands the merge off to GitHub, never reading or sending a merge here", async () => {
    const { fetch, posts } = stubFetch(allMethods);
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    renderMerge(pullRequest(), { caption: true, handoff: { url: PR_URL, unmergedBelow: 2 } });
    const button = screen.getByRole("button", { name: "Merge on GitHub" });
    expect(
      screen.getByText(
        "GitHub merges a stack together: merging this pull request also merges the 2 unmerged pull requests below it.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Squash and merge" })).toBeNull();
    // A page's caption is for a merge made here.
    expect(screen.queryByText(readyCaption.merge)).toBeNull();
    fireEvent.click(button);
    expect(opened).toHaveBeenCalledWith(PR_URL, "_blank", "noopener,noreferrer");
    expect(screen.queryByText("Merge pull request #7?")).toBeNull();
    expect(posts).toEqual([]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("offers no merge while GitHub hasn't said whether it stacks the PR", () => {
    const { fetch } = stubFetch(allMethods);
    renderMerge(pullRequest(), { stackPending: true });
    expect(screen.getByRole("button", { name: "Merge pull request" })).toBeDisabled();
    expect(screen.getByText("Checking how it merges…")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("drops an open merge confirmation once GitHub's stack is pending, then hands off", async () => {
    const { fetch, posts } = stubFetch(allMethods);
    const optionsReads = () =>
      fetch.mock.calls.filter(([input]) => String(input).includes(OPTIONS_PATH)).length;
    const pr = pullRequest();
    const { rerender } = renderMerge(pr);
    await openDialog();
    const confirmButton = within(screen.getByRole("dialog")).getByRole("button", {
      name: "Squash and merge",
    });
    // A retarget starts a new read of GitHub's stack while the dialog is open.
    rerender(pr, { stackPending: true });
    expect(screen.queryByText("Merge pull request #7?")).toBeNull();
    fireEvent.click(confirmButton);
    const readsWhileHeld = optionsReads();
    rerender(pr, { handoff: { url: PR_URL, unmergedBelow: 1 } });
    expect(screen.getByRole("button", { name: "Merge on GitHub" })).toBeInTheDocument();
    expect(screen.queryByText("Merge pull request #7?")).toBeNull();
    // An answer that it merges here after all never brings the old confirmation back.
    rerender(pr);
    expect(await screen.findByRole("button", { name: "Squash and merge" })).toBeEnabled();
    expect(screen.queryByText("Merge pull request #7?")).toBeNull();
    expect(optionsReads()).toBe(readsWhileHeld);
    expect(posts).toEqual([]);
  });

  it("still shows auto-merge that's already on, so it can be turned off", () => {
    stubFetch(allMethods);
    renderMerge(pullRequest({ auto_merge: AUTO_MERGE }), {
      handoff: { url: PR_URL, unmergedBelow: null },
    });
    expect(screen.getByRole("button", { name: /disable auto-merge/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge on GitHub" })).toBeNull();
  });
});

describe("merging as the account the dialog shows", () => {
  const twoAccounts: MergeAccounts = {
    accounts: [
      { login: "octo", active: true, state: "success", host: "github.com" },
      { login: "other", active: false, state: "success", host: "github.com" },
    ],
    selected_account: "octo",
  };

  it("offers a Merge as picker and merges as the account it shows, through merge-as", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), { accounts: twoAccounts });

    await openDialog();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "Merge as" })).toHaveTextContent("octo");
    fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      path: MERGE_AS_PATH,
      body: { pr_url: PR_URL, merge_method: "squash", expected_head: HEAD, account: "octo" },
    });
  });

  it("switches the PR's account after a refusal, then merges as it", async () => {
    // Each account has its own last-used method.
    let defaultMethod: PullRequestMergeMethod = "squash";
    const { posts } = stubFetch(() => mergeOptions(["merge", "squash", "rebase"], defaultMethod)());
    const { client } = renderMerge(pullRequest(), { accounts: twoAccounts });
    const sessionInfo = { object: "session.github.info", selected_pr_url: PR_URL };
    client.setQueryData(["pr-page-github-info", "conv_1"], sessionInfo);

    await openDialog();
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ path: MERGE_AS_PATH, body: { account: "octo" } });
    const refusal = "The GitHub account on the host cannot merge this pull request.";
    await act(async () => {
      posts[0].respond(json({ detail: refusal }, 400));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(refusal);

    const picker = within(dialog).getByRole("combobox", { name: "Merge as" });
    expect(picker).toHaveTextContent("octo");
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("option", { name: "other" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toMatchObject({
      path: PREFERENCES_PATH,
      body: { account: "other", pr_url: PR_URL },
    });
    // The refusal was the previous account's, and no merge starts mid-switch.
    expect(screen.queryByText(refusal)).not.toBeInTheDocument();
    expect(picker).toHaveTextContent("other");
    expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeDisabled();

    defaultMethod = "rebase";
    const prInfo = { object: "session.github.info", selected_pr_url: PR_URL, pr: { number: 7 } };
    await act(async () => {
      posts[1].respond(json(prInfo));
    });
    const confirm = await within(dialog).findByRole("button", { name: "Rebase and merge" });
    expect(confirm).toBeEnabled();
    expect(picker).toHaveTextContent("other");
    // The PR's reply fills that PR's query and leaves the session's alone.
    expect(client.getQueryData(["pr-page-github-info", "conv_1", PR_URL])).toEqual(prInfo);
    expect(client.getQueryData(["pr-page-github-info", "conv_1"])).toEqual(sessionInfo);

    fireEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts[2]).toMatchObject({
      path: MERGE_AS_PATH,
      body: { pr_url: PR_URL, merge_method: "rebase", expected_head: HEAD, account: "other" },
    });
  });

  it("keeps the list the PR's own reads gave it when the switch's reply lists others", async () => {
    const { posts } = stubFetch(allMethods);
    const { client } = renderMerge(pullRequest(), { accounts: twoAccounts });
    const prRecordKey = ["pr-page-github-info", "conv_1", PR_URL];
    const listed = [{ url: PR_URL }];
    client.setQueryData(prRecordKey, { object: "session.github.info", prs: listed });

    await openDialog();
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Merge as" }));
    fireEvent.click(screen.getByRole("option", { name: "other" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    const reply = { object: "session.github.info", selected_pr_url: PR_URL, prs: [] };
    await act(async () => {
      posts[0].respond(json(reply));
    });
    await waitFor(() =>
      expect(client.getQueryData(prRecordKey)).toEqual({ ...reply, prs: listed }),
    );
  });

  it("stays open until an account switch saves", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), { accounts: twoAccounts });

    await openDialog();
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Merge as" }));
    fireEvent.click(screen.getByRole("option", { name: "other" }));
    await waitFor(() => expect(posts).toHaveLength(1));

    // A reopened dialog would race the switch.
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    expect(cancel).toBeDisabled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeDisabled();

    await act(async () => {
      posts[0].respond(json({ object: "session.github.info" }));
    });
    await waitFor(() => expect(cancel).toBeEnabled());
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the plain merge, with no account sent, when there is one account", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), {
      accounts: {
        accounts: [{ login: "octo", active: true, state: "success", host: "github.com" }],
        selected_account: "octo",
      },
    });

    await openDialog();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("combobox", { name: "Merge as" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].path).toBe(MERGE_PATH);
    expect(posts[0].body).not.toHaveProperty("account");
  });

  it("holds Merge when no account is known after a failed switch", async () => {
    stubFetch(allMethods);
    renderMerge(pullRequest(), {
      accounts: { accounts: twoAccounts.accounts, selected_account: null },
    });

    await openDialog();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "Merge as" })).toHaveTextContent(
      "Choose an account",
    );
    expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeDisabled();
  });
});
