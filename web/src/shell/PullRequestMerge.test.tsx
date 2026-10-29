// PullRequestMerge against the real merge hooks, with fetch stubbed: the button
// follows mergeStateStatus and auto-merge, names the repository's default method,
// and a merge posts once, pinned to the head the dialog opened on.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PullRequest, PullRequestMergeMethod } from "@/hooks/usePullRequests";
import { mergeButtonState, PullRequestMerge } from "./PullRequestMerge";

const PR_URL = "https://github.com/acme/app/pull/7";
const MERGE_PATH = "/v1/sessions/conv_1/resources/github/merge";
const DISABLE_PATH = "/v1/sessions/conv_1/resources/github/disable-auto-merge";
const OPTIONS_PATH = "/v1/sessions/conv_1/resources/github/merge-options";
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

function renderMerge(pr: PullRequest, compact = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const tree = (next: PullRequest) => (
    <QueryClientProvider client={client}>
      <PullRequestMerge conversationId="conv_1" prUrl={PR_URL} pr={next} compact={compact} />
    </QueryClientProvider>
  );
  const view = render(tree(pr));
  return { invalidate, rerender: (next: PullRequest) => view.rerender(tree(next)) };
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
