// PullRequestReviewers against the real reviewer hooks, with fetch stubbed: an edit
// shows as in progress until the host answers, settles to the reviewers it reports, and
// a refused edit rolls back with the host's message.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  PullRequestInfo,
  PullRequestReviewCandidate,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  requestPullRequestControl,
  resetControlRequestsForTests,
} from "@/pullRequestPage/controlRequests";
import { PullRequestDestinationContext } from "@/pullRequestPage/pullRequestDestination";
import { PullRequestReviewers } from "./PullRequestReviewers";
import { typedReviewer } from "../../shell/ReviewerPickerList";
import { ANNOUNCE_REFILL_DELAY_MS } from "@/components/pendingChange/announceChange";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const INFO_KEY = ["pr-page-github-info", "conv_1", PR_URL];
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

/** What the live region last said, once its microtask has run. */
function announced(): string {
  return document.querySelector('[data-testid="change-announcer"]')?.textContent ?? "";
}

/** What the live region says once its pending refill lands. */
async function spoken(): Promise<string> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, ANNOUNCE_REFILL_DELAY_MS);
      }),
  );
  return announced();
}

function shownReviewers(): string[] {
  const section = screen.getByRole("heading", { name: "Reviewers" }).closest("section")!;
  return within(section)
    .queryAllByRole("listitem")
    .map((item) => item.getAttribute("aria-label") ?? "");
}

/** Close the open picker as Escape does. */
function closePicker() {
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Filter reviewers" }), {
    key: "Escape",
  });
}

it("checks and unchecks while open, then sends every change at once when it closes", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);

  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  const option = await screen.findByRole("option", { name: /^alice/ });
  // The PR's author can't review it, so the picker leaves them out.
  expect(screen.queryByRole("option", { name: /^dev/ })).toBeNull();
  expect(screen.getByRole("option", { name: /^Team core/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(option).toHaveAttribute("aria-checked", "false");
  fireEvent.click(option);
  fireEvent.click(screen.getByRole("option", { name: /^Team core/ }));

  // Nothing is sent, and nothing waits, while the picker stays open.
  expect(screen.getByRole("option", { name: /^alice/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("option", { name: /^Team core/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  expect(screen.getByRole("option", { name: /^alice/ })).not.toHaveAttribute(
    "data-disabled",
    "true",
  );
  expect(posts).toHaveLength(0);
  expect(shownReviewers()).toEqual(["Team core: Awaiting review"]);
  expect(screen.getByText("1 to request, 1 to withdraw · sent when you close")).toBeVisible();

  closePicker();
  await waitFor(() =>
    expect(shownReviewers()).toEqual(["Team core: Withdrawing…", "alice: Requesting…"]),
  );
  const row = screen.getByRole("listitem", { name: "alice: Requesting…" });
  expect(row).toHaveAttribute("aria-busy", "true");
  expect(within(row).queryByRole("button")).toBeNull();
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({
    pr_url: PR_URL,
    add: [{ login: "alice", kind: "user" }],
    remove: [{ login: "core", kind: "team" }],
  });
  expect(await spoken()).toBe(
    "Requesting a review from alice; withdrawing the review request from Team core…",
  );

  const reported: PullRequestReviewer[] = [{ login: "alice", kind: "user", state: "PENDING" }];
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_reviewers", reviewers: reported })),
  );

  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.reviewers).toEqual(reported),
  );
  expect(shownReviewers()).toEqual(["alice: Awaiting review"]);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(posts).toHaveLength(1);
});

it("lists the reviewers and teams already asked first, and keeps the order while checking more", async () => {
  // Many people to offer, so one asked late in the list would sit far below the fold.
  const many = Array.from({ length: 60 }, (_, index) => ({
    login: `person-${String(index).padStart(2, "0")}`,
    kind: "user" as const,
    name: null,
  }));
  stubFetch(() => json({ object: "list", data: [...many, alice, core], has_more: false }));
  renderReviewers([
    { login: "person-00", kind: "user", state: "APPROVED" },
    { login: "person-01", kind: "user", state: "COMMENTED" },
    { login: "person-59", kind: "user", state: "PENDING" },
    { login: "core", kind: "team", state: "PENDING" },
    { login: "alice", kind: "user", state: "PENDING" },
  ]);
  // Each row's login, without the name shown under it.
  const loginOf = (option: HTMLElement) => option.querySelector("span span")?.textContent;
  const options = () => screen.getAllByRole("option").map(loginOf);
  const requested = () =>
    within(screen.getByRole("group", { name: "Requested" }))
      .getAllByRole("option")
      .map(loginOf);

  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  await screen.findByRole("option", { name: /^person-30/ });
  // Asked first, people and teams alike, in the PR's own order; then everyone else.
  expect(requested()).toEqual(["person-59", "Team core", "alice"]);
  expect(options().slice(0, 5)).toEqual([
    "person-59",
    "Team core",
    "alice",
    "person-00",
    "person-01",
  ]);

  // Checking someone while it's open moves no rows under the pointer.
  fireEvent.click(screen.getByRole("option", { name: /^person-01/ }));
  fireEvent.click(screen.getByRole("option", { name: /^Team core/ }));
  expect(options().slice(0, 5)).toEqual([
    "person-59",
    "Team core",
    "alice",
    "person-00",
    "person-01",
  ]);

  // A filter ranks by match, as it did, with no Requested group.
  fireEvent.change(screen.getByRole("combobox", { name: "Filter reviewers" }), {
    target: { value: "person-0" },
  });
  expect(screen.queryByRole("group", { name: "Requested" })).toBeNull();
  expect(options()[0]).toBe("person-00");
});

it("tells a requested user and team of the same name apart, and lists each once after a refresh", async () => {
  stubFetch(candidates);
  const client = renderReviewers([
    { login: "alice", kind: "user", state: "PENDING" },
    { login: "core", kind: "user", state: "PENDING" },
    { login: "core", kind: "team", state: "PENDING" },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  await screen.findByRole("option", { name: /^alice/ });
  const requested = () => within(screen.getByRole("group", { name: "Requested" }));
  // Each is named for what it is, so withdrawing one never means guessing which.
  expect(requested().getByRole("option", { name: /^core$/ })).toBeVisible();
  expect(requested().getByRole("option", { name: /^Team core/ })).toBeVisible();

  // GitHub answers again with the same requests in another order.
  act(() =>
    client.setQueryData<PullRequestInfo>(INFO_KEY, (info) => ({
      ...info!,
      pr: {
        ...info!.pr!,
        reviewers: [
          { login: "core", kind: "user", state: "PENDING" },
          { login: "core", kind: "team", state: "PENDING" },
          { login: "alice", kind: "user", state: "PENDING" },
        ],
      },
    })),
  );
  await waitFor(() => expect(requested().getAllByRole("option")).toHaveLength(3));
  expect(screen.getAllByRole("option", { name: /^core$/ })).toHaveLength(1);
  expect(screen.getAllByRole("option", { name: /^Team core/ })).toHaveLength(1);
});

it("sends nothing when the picker closes with everything as it was", async () => {
  const posts = stubFetch(candidates);
  renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);

  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  const option = await screen.findByRole("option", { name: /^alice/ });
  fireEvent.click(option);
  fireEvent.click(screen.getByRole("option", { name: /^alice/ }));
  expect(screen.getByRole("option", { name: /^alice/ })).toHaveAttribute("aria-checked", "false");
  closePicker();

  await waitFor(() =>
    expect(screen.queryByRole("combobox", { name: "Filter reviewers" })).toBeNull(),
  );
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
  });
  expect(posts).toHaveLength(0);
  expect(shownReviewers()).toEqual(["Team core: Awaiting review"]);
});

it("keeps a reviewer in place while a re-request or withdrawal is in progress", async () => {
  const posts = stubFetch(candidates);
  renderReviewers([
    { login: "alice", kind: "user", state: "APPROVED" },
    { login: "core", kind: "team", state: "PENDING" },
  ]);

  fireEvent.click(screen.getByRole("button", { name: "Re-request review from alice" }));
  await waitFor(() =>
    expect(shownReviewers()).toEqual(["alice: Requesting…", "Team core: Awaiting review"]),
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
    expect(shownReviewers()).toEqual(["alice: Awaiting review", "Team core: Withdrawing…"]),
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
  await waitFor(() => expect(shownReviewers()).toEqual(["alice: Awaiting review"]));
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
  fireEvent.click(screen.getByRole("option", { name: /^Team platform\s*Not in list/ }));
  expect(screen.getByText("GitHub checks it when you close")).toBeVisible();
  // Checked, it stays listed whatever the filter says next, until the picker closes.
  fireEvent.change(screen.getByRole("combobox", { name: "Filter reviewers" }), {
    target: { value: "" },
  });
  expect(screen.getByRole("option", { name: /^Team platform\s*Not in list/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByText("1 to request · sent when you close")).toBeVisible();
  closePicker();

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
  expect(shownReviewers()).toEqual(["Team core: Awaiting review"]);
  expect(client.getQueryState(INFO_KEY)?.isInvalidated).toBe(true);
});

it("keeps a refused withdrawal's failure after its section remounts, until Retry goes through", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);
  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(await spoken()).toBe("Withdrawing the review request from Team core…");
  await act(async () => posts[0].respond(json({ detail: "Invalid request." }, 400)));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t withdraw the review request from Team core: Invalid request.",
  );
  // Said once, by the alert: the polite region doesn't repeat it.
  expect(await spoken()).not.toMatch(/Couldn’t/);

  // The PR was closed and opened again: the failure is still said, read from the cache.
  cleanup();
  render(
    <QueryClientProvider client={client}>
      <CachedReviewers />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("Invalid request.");

  // Retry is a fresh change, sent as the targets alone.
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].body.remove).toEqual([{ login: "core", kind: "team" }]);
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () =>
    posts[1].respond(json({ object: "session.github.pr_reviewers", reviewers: [] })),
  );
  await waitFor(() => expect(shownReviewers()).toEqual([]));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(await spoken()).toBe("Withdrew the review request from Team core.");
});

it("forgets repeated refusals with one Dismiss, also after a remount", async () => {
  const posts = stubFetch(candidates);
  const client = renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);
  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => posts[0].respond(json({ detail: "First refusal." }, 400)));
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  await act(async () => posts[1].respond(json({ detail: "Second refusal." }, 400)));
  expect(await screen.findByRole("alert")).toHaveTextContent("Second refusal.");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  // The first refusal doesn't come back in its place.
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  cleanup();
  render(
    <QueryClientProvider client={client}>
      <CachedReviewers />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("alert")).toBeNull();
});

it("forgets a refusal on Dismiss", async () => {
  const posts = stubFetch(candidates);
  renderReviewers([{ login: "core", kind: "team", state: "PENDING" }]);
  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => posts[0].respond(json({ detail: "Invalid request." }, 400)));
  fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
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
  expect(screen.getByRole("listitem", { name: "alice: Awaiting review" })).toBeInTheDocument();
  expect(screen.queryByRole("button")).toBeNull();
});

describe("pressing anywhere on the section", () => {
  const picker = () => screen.queryByPlaceholderText("Filter or type a login…");

  it("opens the picker from the title, a reviewer's row or empty space, and starts unfiltered", async () => {
    stubFetch(candidates);
    renderReviewers([{ login: "alice", kind: "user", state: "APPROVED" }]);
    fireEvent.click(screen.getByRole("heading", { name: "Reviewers" }));
    await waitFor(() => expect(picker()).not.toBeNull());
    fireEvent.change(picker()!, { target: { value: "bo" } });
    fireEvent.keyDown(picker()!, { key: "Escape" });
    await waitFor(() => expect(picker()).toBeNull());
    expect(screen.getByRole("button", { name: "Request reviewers" })).toHaveFocus();

    fireEvent.click(screen.getByText("alice"));
    await waitFor(() => expect(picker()).toHaveValue(""));
  });

  it("leaves a reviewer's own action to re-request, without opening the picker", async () => {
    const posts = stubFetch(candidates);
    renderReviewers([{ login: "alice", kind: "user", state: "APPROVED" }]);
    fireEvent.click(screen.getByRole("button", { name: "Re-request review from alice" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(picker()).toBeNull();
  });

  it("is inert where reviewers can't be edited", async () => {
    stubFetch(candidates);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <PullRequestReviewers
          conversationId="conv_1"
          prUrl={PR_URL}
          reviewers={[]}
          author={null}
          editable={false}
        />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByText("No reviewers"));
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(picker()).toBeNull();
  });
});

describe("opening from a row's Request Review…", () => {
  // The sidebar's copy renders first, so it would take a request it wasn't aimed at.
  const VIEWS = [
    { id: "sidebar", active: true },
    { id: "detail", active: true },
  ];
  afterEach(() => resetControlRequestsForTests());
  const trigger = (id: string) =>
    within(screen.getByTestId(id)).getByRole("button", { name: "Request reviewers" });

  it("waits for the view's section to load and be editable, and opens only that copy", async () => {
    stubFetch(candidates);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (editable: boolean) => (
      <QueryClientProvider client={client}>
        {VIEWS.map((view) => (
          <PullRequestDestinationContext.Provider key={view.id} value={view}>
            <div data-testid={view.id}>
              <PullRequestReviewers
                conversationId="conv_1"
                prUrl={PR_URL}
                reviewers={[]}
                author="dev"
                editable={editable}
              />
            </div>
          </PullRequestDestinationContext.Provider>
        ))}
      </QueryClientProvider>
    );
    const view = render(tree(false));
    act(() => requestPullRequestControl(PR_URL, "reviewers", { destination: "detail" }, null));
    // The PR's facts arrive after the row asked.
    view.rerender(tree(true));
    await waitFor(() => expect(trigger("detail")).toHaveAttribute("aria-expanded", "true"));
    expect(trigger("sidebar")).toHaveAttribute("aria-expanded", "false");
  });
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
