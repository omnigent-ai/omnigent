// PullRequestReviewers against the real reviewer hooks, with fetch stubbed: an edit
// shows as in progress until the host answers, settles to the reviewers it reports, and
// a refused edit rolls back with the host's message.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  PullRequestInfo,
  PullRequestReviewCandidate,
  PullRequestReviewer,
} from "@/hooks/usePullRequests";
import { PullRequestReviewers, typedReviewer } from "./PullRequestReviewers";

const PR_URL = "https://github.com/acme/app/pull/7";
const INFO_KEY = ["github-info", "conv_1", PR_URL];
const REVIEWERS_PATH = "/v1/sessions/conv_1/resources/github/reviewers";
const alice: PullRequestReviewCandidate = { login: "alice", kind: "user", name: "Alice Liddell" };
const dev: PullRequestReviewCandidate = { login: "dev", kind: "user", name: null };
const core: PullRequestReviewCandidate = { login: "core", kind: "team", name: "Core" };

afterEach(() => vi.unstubAllGlobals());

interface ReviewerPost {
  body: { pr_url: string; add: unknown[]; remove: unknown[] };
  respond: (response: Response) => void;
}

/** Stub fetch: reviewer posts wait for the test to answer; the candidate list answers `listed`. */
function stubFetch(listed: () => Response) {
  const posts: ReviewerPost[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname !== REVIEWERS_PATH) {
        return Promise.resolve(new Response("{}", { status: 404 }));
      }
      if (init?.method !== "POST") return Promise.resolve(listed());
      return new Promise<Response>((respond) => {
        posts.push({ body: JSON.parse(String(init.body)), respond });
      });
    }),
  );
  return posts;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const candidates = () => json({ object: "list", data: [alice, dev, core], has_more: false });

/** The section fed from the info cache, as the panel feeds it. */
function CachedReviewers() {
  const { data } = useQuery<PullRequestInfo>({
    queryKey: INFO_KEY,
    queryFn: () => new Promise(() => {}),
    enabled: false,
  });
  return (
    <PullRequestReviewers
      conversationId="conv_1"
      prUrl={PR_URL}
      reviewers={data?.pr?.reviewers ?? []}
      author="dev"
      editable
    />
  );
}

function renderReviewers(reviewers: PullRequestReviewer[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData<PullRequestInfo>(INFO_KEY, {
    object: "session.github.info",
    available: true,
    selected_pr_url: PR_URL,
    pr: {
      number: 7,
      title: "Reviewers",
      state: "OPEN",
      url: PR_URL,
      is_draft: false,
      author: "dev",
      base_ref: "main",
      head_ref: "reviewers",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
      reviewers,
    },
  });
  render(
    <QueryClientProvider client={client}>
      <CachedReviewers />
    </QueryClientProvider>,
  );
  return client;
}

function shownReviewers(): string[] {
  const section = screen.getByRole("heading", { name: "Reviewers" }).closest("section")!;
  return within(section)
    .queryAllByRole("listitem")
    .map((item) => item.getAttribute("aria-label") ?? "");
}

it("shows a request as in progress until the host reports the reviewers", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);

  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  const option = await screen.findByRole("option", { name: /^alice/ });
  // The PR's author can't review it, so the picker leaves them out.
  expect(screen.queryByRole("option", { name: /^dev/ })).toBeNull();
  expect(screen.getByRole("option", { name: /^core/ })).toHaveAttribute("data-checked", "true");
  expect(option).toHaveAttribute("data-checked", "false");
  fireEvent.click(option);

  await waitFor(() =>
    expect(shownReviewers()).toEqual(["Team core: Pending", "alice: Requesting…"]),
  );
  const row = screen.getByRole("listitem", { name: "alice: Requesting…" });
  expect(row).toHaveAttribute("aria-busy", "true");
  expect(within(row).queryByRole("button")).toBeNull();
  expect(
    within(screen.getByRole("option", { name: /^alice/ })).getByLabelText("Requesting…"),
  ).toBeInTheDocument();
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({
    pr_url: PR_URL,
    add: [{ login: "alice", kind: "user" }],
    remove: [],
  });

  const reported: PullRequestReviewer[] = [
    { login: "alice", kind: "user", state: "PENDING" },
    { login: "core", kind: "team", state: "PENDING" },
  ];
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_reviewers", reviewers: reported })),
  );

  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.reviewers).toEqual(reported),
  );
  expect(shownReviewers()).toEqual(["alice: Pending", "Team core: Pending"]);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keeps a reviewer in place while a re-request or withdrawal is in progress", async () => {
  const posts = stubFetch(candidates);
  renderReviewers([
    { login: "alice", kind: "user", state: "APPROVED" },
    { login: "core", kind: "team", state: "PENDING" },
  ]);

  fireEvent.click(screen.getByRole("button", { name: "Re-request review from alice" }));
  await waitFor(() =>
    expect(shownReviewers()).toEqual(["alice: Requesting…", "Team core: Pending"]),
  );
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body.add).toEqual([{ login: "alice", kind: "user" }]);
  await act(async () =>
    posts[0].respond(
      json({
        object: "session.github.pr_reviewers",
        reviewers: [
          { login: "alice", kind: "user", state: "PENDING" },
          { login: "core", kind: "team", state: "PENDING" },
        ],
      }),
    ),
  );

  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  await waitFor(() =>
    expect(shownReviewers()).toEqual(["alice: Pending", "Team core: Withdrawing…"]),
  );
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].body).toEqual({
    pr_url: PR_URL,
    add: [],
    remove: [{ login: "core", kind: "team" }],
  });
  await act(async () =>
    posts[1].respond(
      json({
        object: "session.github.pr_reviewers",
        reviewers: [{ login: "alice", kind: "user", state: "PENDING" }],
      }),
    ),
  );
  await waitFor(() => expect(shownReviewers()).toEqual(["alice: Pending"]));
});

it("requests a typed login that the candidate list does not include", async () => {
  const posts = stubFetch(() => json({ object: "list", data: [alice], has_more: true }));
  renderReviewers([]);

  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  expect(
    await screen.findByText("Showing the first 1. Type a login to request anyone else."),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Filter reviewers" }), {
    target: { value: "acme/platform" },
  });
  fireEvent.click(screen.getByRole("option", { name: /^Request review from Team platform/ }));

  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body.add).toEqual([{ login: "platform", kind: "team" }]);
  expect(shownReviewers()).toEqual(["Team platform: Requesting…"]);
});

it("rolls back a refused withdrawal, shows why, and refreshes the PR", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);

  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  await waitFor(() => expect(shownReviewers()).toEqual(["Team core: Withdrawing…"]));
  await waitFor(() => expect(posts).toHaveLength(1));
  const detail = "GitHub rejected the reviewer change: Invalid request.";
  await act(async () => posts[0].respond(json({ detail }, 400)));

  expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  expect(shownReviewers()).toEqual(["Team core: Pending"]);
  expect(client.getQueryState(INFO_KEY)?.isInvalidated).toBe(true);
});

it("rolls back a refused request, shows why, and refreshes the PR", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "alice", kind: "user", state: "COMMENTED" }]);

  fireEvent.click(screen.getByRole("button", { name: "Re-request review from alice" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  const detail =
    "GitHub rejected the reviewer change: Reviews may only be requested from collaborators.";
  await act(async () => posts[0].respond(json({ detail }, 400)));

  expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  expect(shownReviewers()).toEqual(["alice: Commented"]);
  expect(client.getQueryState(INFO_KEY)?.isInvalidated).toBe(true);
});

it("shows reviewers read-only without editing or a selected PR", () => {
  stubFetch(candidates);
  const client = new QueryClient();
  const reviewers: PullRequestReviewer[] = [{ login: "alice", kind: "user", state: "PENDING" }];
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <PullRequestReviewers
        conversationId="conv_1"
        prUrl={PR_URL}
        reviewers={reviewers}
        author={null}
        editable={false}
      />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("button")).toBeNull();

  rerender(
    <QueryClientProvider client={client}>
      <PullRequestReviewers
        conversationId="conv_1"
        prUrl={null}
        reviewers={reviewers}
        author={null}
        editable
      />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("listitem", { name: "alice: Pending" })).toBeInTheDocument();
  expect(screen.queryByRole("button")).toBeNull();
});

describe("typedReviewer", () => {
  it.each([
    ["octocat", { login: "octocat", kind: "user" }],
    [" @Octo-Cat ", { login: "Octo-Cat", kind: "user" }],
    ["enterprise_user", { login: "enterprise_user", kind: "user" }],
    ["acme/core.team", { login: "core.team", kind: "team" }],
    ["", null],
    ["two words", null],
    ["-leading", null],
    ["a/b/c", null],
  ])("reads %j", (query, expected) => {
    expect(typedReviewer(query)).toEqual(expected);
  });
});
