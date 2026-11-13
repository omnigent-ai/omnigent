// PullRequestMerge against the real merge hooks, with fetch stubbed: the button
// follows mergeStateStatus and auto-merge, names the repository's default method,
// and a merge posts once, pinned to the head the dialog opened on.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PullRequest, PullRequestInfo, PullRequestMergeMethod } from "@/hooks/usePullRequests";
import { mergeButtonState, PullRequestMerge, type MergeAccounts } from "./PullRequestMerge";

const PR_URL = "https://github.com/acme/app/pull/7";
const MERGE_PATH = "/v1/sessions/conv_1/resources/github/merge";
const MERGE_AS_PATH = "/v1/sessions/conv_1/resources/github/merge-as";
const DISABLE_PATH = "/v1/sessions/conv_1/resources/github/disable-auto-merge";
const OPTIONS_PATH = "/v1/sessions/conv_1/resources/github/merge-options";
const PREFERENCES_PATH = "/v1/sessions/conv_1/resources/github/preferences";
const INFO_PATH = "/v1/sessions/conv_1/resources/github";
const HEAD = "a".repeat(40);
const AUTO_MERGE = { merge_method: "MERGE", enabled_by: "dev", enabled_at: "2026-10-21T06:02:55Z" };

afterEach(() => vi.unstubAllGlobals());

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
function stubFetch(options: () => Response, info?: () => Promise<Response>) {
  const posts: Post[] = [];
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === OPTIONS_PATH) return Promise.resolve(options());
    if (url.pathname === INFO_PATH && info) return info();
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

function renderMerge(pr: PullRequest, compact = false, accounts?: MergeAccounts) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const tree = (next: PullRequest) => (
    <QueryClientProvider client={client}>
      <PullRequestMerge
        conversationId="conv_1"
        prUrl={PR_URL}
        pr={next}
        compact={compact}
        accounts={accounts}
      />
    </QueryClientProvider>
  );
  const view = render(tree(pr));
  return { client, invalidate, rerender: (next: PullRequest) => view.rerender(tree(next)) };
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

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["github-info", "conv_1"] });
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

it.each([false, true])("merges once at the confirmed head (compact: %s)", async (compact) => {
  const { posts } = stubFetch(allMethods);
  const { invalidate, rerender } = renderMerge(pullRequest(), compact);

  await openDialog(compact ? "Merge" : undefined);
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
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["github-info", "conv_1"] });
  expect(posts).toHaveLength(1);
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

describe("merging as another account", () => {
  const twoAccounts: MergeAccounts = {
    accounts: [
      { login: "work", active: true, state: "success", host: "github.com" },
      { login: "personal", active: false, state: "success", host: "github.com" },
    ],
    selected_account: "work",
  };

  it("switches the PR's account after a refusal, then merges as it", async () => {
    // Each account has its own last-used method.
    let defaultMethod: PullRequestMergeMethod = "squash";
    const { posts } = stubFetch(() => mergeOptions(["merge", "squash", "rebase"], defaultMethod)());
    const { client } = renderMerge(pullRequest(), false, twoAccounts);
    const sessionInfo = { object: "session.github.info", selected_pr_url: PR_URL };
    client.setQueryData(["github-info", "conv_1"], sessionInfo);

    await openDialog();
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ path: MERGE_AS_PATH, body: { account: "work" } });
    const refusal = "The GitHub account on the host cannot merge this pull request.";
    await act(async () => {
      posts[0].respond(json({ detail: refusal }, 400));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(refusal);

    const picker = within(dialog).getByRole("combobox", { name: "Merge as" });
    expect(picker).toHaveTextContent("work");
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("option", { name: "personal" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toMatchObject({
      path: PREFERENCES_PATH,
      body: { account: "personal", pr_url: PR_URL },
    });
    // The refusal was the previous account's, and no merge starts mid-switch.
    expect(screen.queryByText(refusal)).not.toBeInTheDocument();
    expect(picker).toHaveTextContent("personal");
    expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeDisabled();

    defaultMethod = "rebase";
    const prInfo = { object: "session.github.info", selected_pr_url: PR_URL, pr: { number: 7 } };
    await act(async () => {
      posts[1].respond(json(prInfo));
    });
    const confirm = await within(dialog).findByRole("button", { name: "Rebase and merge" });
    expect(confirm).toBeEnabled();
    expect(picker).toHaveTextContent("personal");
    // The PR's reply fills that PR's query and leaves the session's alone.
    expect(client.getQueryData(["github-info", "conv_1", PR_URL])).toEqual(prInfo);
    expect(client.getQueryData(["github-info", "conv_1"])).toEqual(sessionInfo);

    fireEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts[2]).toMatchObject({
      path: MERGE_AS_PATH,
      body: { pr_url: PR_URL, merge_method: "rebase", expected_head: HEAD, account: "personal" },
    });
  });

  it("stays open until an account switch saves", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), false, twoAccounts);

    await openDialog();
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Merge as" }));
    fireEvent.click(screen.getByRole("option", { name: "personal" }));
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

  /** The dialog fed from the PR's info query, as the panel feeds it. */
  function MergeFromInfo() {
    const { data } = useQuery({
      queryKey: ["github-info", "conv_1", PR_URL],
      queryFn: async () => {
        const response = await fetch(INFO_PATH);
        if (!response.ok) throw new Error("info failed");
        return (await response.json()) as PullRequestInfo;
      },
      retry: false,
    });
    return (
      <PullRequestMerge
        conversationId="conv_1"
        prUrl={PR_URL}
        pr={pullRequest()}
        accounts={data?.auth ?? undefined}
      />
    );
  }

  it.each([
    ["names the account", ["work", "personal"], true],
    ["fails too", null, true],
    // An account lookup can fail while the PR still reads; a merge then stays bound.
    ["lists no accounts", [], false],
    ["lists only another account", ["work"], false],
    ["lists only the saved account", ["personal"], true],
  ] as const)(
    "fails closed after a failed switch until the refetch %s",
    async (_refetch, logins, merges) => {
      const infoRequests: ((response: Response) => void)[] = [];
      const { fetch, posts } = stubFetch(
        allMethods,
        () =>
          new Promise((respond) => {
            infoRequests.push(respond);
          }),
      );
      const info = (selected: string, listed: readonly string[] = ["work", "personal"]) =>
        json({
          object: "session.github.info",
          selected_pr_url: PR_URL,
          auth: {
            accounts: twoAccounts.accounts!.filter((account) => listed.includes(account.login)),
            selected_account: selected,
          },
        });
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <MergeFromInfo />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(infoRequests).toHaveLength(1));
      await act(async () => infoRequests[0](info("work")));
      const optionLoads = () =>
        fetch.mock.calls.filter(([input]) => String(input).startsWith(OPTIONS_PATH)).length;

      await openDialog();
      const dialog = screen.getByRole("dialog");
      const picker = within(dialog).getByRole("combobox", { name: "Merge as" });
      await waitFor(() => expect(picker).toHaveTextContent("work"));
      const loadsBefore = optionLoads();
      fireEvent.click(picker);
      fireEvent.click(screen.getByRole("option", { name: "personal" }));
      await waitFor(() => expect(posts).toHaveLength(1));
      // The host saved `personal`, then the reply failed.
      await act(async () => {
        posts[0].respond(json({ detail: "Host offline" }, 400));
      });

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Couldn’t switch accounts: Host offline",
      );
      // Neither account is known to be the saved one, so nothing can merge.
      expect(picker).toHaveTextContent("Choose an account");
      const confirm = within(dialog).getByRole("button", { name: "Squash and merge" });
      expect(confirm).toBeDisabled();
      // The failure still refetches the PR's info and the account's merge options.
      await waitFor(() => expect(infoRequests).toHaveLength(2));
      await waitFor(() => expect(optionLoads()).toBeGreaterThan(loadsBefore));

      if (logins) {
        await act(async () => infoRequests[1](info("personal", logins)));
        await waitFor(() =>
          expect(
            client.getQueryData<PullRequestInfo>(["github-info", "conv_1", PR_URL])?.auth?.accounts,
          ).toHaveLength(logins.length),
        );
      }
      if (!merges) {
        // Nothing on screen names the account, so the dialog can't merge, even unbound.
        expect(picker).toHaveTextContent("Choose an account");
        expect(confirm).toBeDisabled();
        expect(posts.map((post) => post.path)).toEqual([PREFERENCES_PATH]);
        return;
      }
      if (logins) {
        await waitFor(() => expect(picker).toHaveTextContent("personal"));
      } else {
        await act(async () => infoRequests[1](json({ detail: "Host offline" }, 503)));
        expect(confirm).toBeDisabled();
        // Retrying the same account fails again: the picker goes back to no account.
        fireEvent.click(picker);
        fireEvent.click(screen.getByRole("option", { name: "personal" }));
        await waitFor(() => expect(posts).toHaveLength(2));
        expect(posts[1].path).toBe(PREFERENCES_PATH);
        await act(async () => posts[1].respond(json({ detail: "Host offline" }, 400)));
        await waitFor(() => expect(picker).toHaveTextContent("Choose an account"));
        expect(confirm).toBeDisabled();
        // Choosing it once more still sends a save, which settles it.
        fireEvent.click(picker);
        fireEvent.click(screen.getByRole("option", { name: "personal" }));
        await waitFor(() => expect(posts).toHaveLength(3));
        expect(posts[2].path).toBe(PREFERENCES_PATH);
        await act(async () => posts[2].respond(info("personal")));
        expect(picker).toHaveTextContent("personal");
      }
      await waitFor(() => expect(confirm).toBeEnabled());
      fireEvent.click(confirm);
      await waitFor(() => expect(posts.at(-1)?.path).toBe(MERGE_AS_PATH));
      expect(posts.at(-1)?.body).toMatchObject({ account: "personal" });
    },
  );

  it("offers no account choice with one signed-in account", async () => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), false, {
      accounts: [twoAccounts.accounts![0]],
      selected_account: "work",
    });

    await openDialog();
    expect(screen.queryByRole("combobox", { name: "Merge as" })).not.toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Squash and merge" }),
    );
    await waitFor(() => expect(posts).toHaveLength(1));
    // Nothing to choose, so the host keeps its own account.
    expect(posts[0].path).toBe(MERGE_PATH);
    expect(posts[0].body).not.toHaveProperty("account");
  });

  it.each([
    ["server or runner", json({ detail: "Not Found" }, 404)],
    ["host", json({ detail: "unknown fs write op: 'github_merge_as'" }, 400)],
  ])("refuses, never merging as the saved account, behind an older %s", async (_peer, reply) => {
    const { posts } = stubFetch(allMethods);
    renderMerge(pullRequest(), false, twoAccounts);

    await openDialog();
    const dialog = screen.getByRole("dialog");
    // The switch saves; another tab could save a different account after it.
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Merge as" }));
    fireEvent.click(screen.getByRole("option", { name: "personal" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    await act(async () => posts[0].respond(json({ object: "session.github.info" })));
    const confirm = within(dialog).getByRole("button", { name: "Squash and merge" });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(2));
    await act(async () => posts[1].respond(reply));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "can’t merge as a chosen account. Update them, or merge on GitHub.",
    );
    expect(posts.map((post) => post.path)).toEqual([PREFERENCES_PATH, MERGE_AS_PATH]);
  });
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
