// PullRequestLabels against the real label hooks, with fetch stubbed: an edit shows as
// in progress until the host answers, edits run one at a time, settle to the labels it
// reports, and a refused edit rolls back with the host's message and a Retry.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PullRequestInfo, PullRequestLabel } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  requestPullRequestControl,
  resetControlRequestsForTests,
} from "@/pullRequestPage/controlRequests";
import { PullRequestDestinationContext } from "@/pullRequestPage/pullRequestDestination";
import { PullRequestLabels } from "./PullRequestLabels";
import { ANNOUNCE_REFILL_DELAY_MS } from "@/components/pendingChange/announceChange";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const INFO_KEY = ["pr-page-github-info", "conv_1", PR_URL];
const LABELS_PATH = "/v1/sessions/conv_1/resources/github/labels";
const bug: PullRequestLabel = { name: "bug", color: "d73a4a", description: null };
const ready: PullRequestLabel = { name: "ready", color: "0e8a16", description: "Ship it" };

afterEach(() => vi.unstubAllGlobals());

interface LabelPost {
  body: { pr_url: string; add: string[]; remove: string[] };
  respond: (response: Response) => void;
}

/** Stub fetch: label posts wait for the test to answer; the repo list answers `repoLabels`. */
function stubFetch(repoLabels: () => Response) {
  const posts: LabelPost[] = [];
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname !== LABELS_PATH) {
      return Promise.resolve(new Response("{}", { status: 404 }));
    }
    if (init?.method !== "POST") return Promise.resolve(repoLabels());
    return new Promise<Response>((respond) => {
      posts.push({ body: JSON.parse(String(init.body)), respond });
    });
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, posts };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

/** The section fed from the info cache, as the panel feeds it. */
function CachedLabels() {
  const { data } = useQuery<PullRequestInfo>({
    queryKey: INFO_KEY,
    queryFn: () => new Promise(() => {}),
    enabled: false,
  });
  return (
    <PullRequestLabels
      conversationId="conv_1"
      prUrl={PR_URL}
      labels={data?.pr?.labels ?? []}
      editable
    />
  );
}

function renderLabels(labels: PullRequestLabel[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData<PullRequestInfo>(INFO_KEY, {
    object: "session.github.info",
    available: true,
    selected_pr_url: PR_URL,
    pr: {
      number: 7,
      title: "Labels",
      state: "OPEN",
      url: PR_URL,
      is_draft: false,
      author: "dev",
      base_ref: "main",
      head_ref: "labels",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
      labels,
    },
  });
  render(
    <QueryClientProvider client={client}>
      <CachedLabels />
    </QueryClientProvider>,
  );
  return client;
}

/** Each chip's name, with its edit while one is in flight, e.g. "bug: Removing…". */
function shownLabels(): string[] {
  const list = screen.queryByRole("list", { name: "Labels" });
  return list
    ? within(list)
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("aria-label") ?? item.textContent ?? "")
    : [];
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

/** Close the picker as Esc does; its checks are applied then. */
function closePicker() {
  fireEvent.keyDown(screen.getByPlaceholderText("Filter labels…"), { key: "Escape" });
}

it("applies the picker's checks when it closes, and keeps the labels the host reports", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
  const client = renderLabels([bug]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  expect(await screen.findByRole("option", { name: /^bug/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByRole("option", { name: /^ready/ })).toHaveAttribute("aria-checked", "false");
  fireEvent.click(screen.getByRole("option", { name: /^ready/ }));
  // Checked, but nothing is sent while the picker is open.
  expect(screen.getByRole("option", { name: /^ready/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByText("1 to add · applied when you close")).toBeInTheDocument();
  expect(shownLabels()).toEqual(["bug"]);

  closePicker();
  await waitFor(() => expect(shownLabels()).toEqual(["bug", "ready: Adding…"]));
  const chip = screen.getByRole("listitem", { name: "ready: Adding…" });
  expect(chip).toHaveAttribute("aria-busy", "true");
  expect(within(chip).queryByRole("button")).toBeNull();
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: ["ready"], remove: [] });
  expect(await spoken()).toBe("Adding the label ready…");

  const reported = { ...ready, description: "Reported by GitHub" };
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_labels", labels: [bug, reported] })),
  );

  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.labels).toEqual([bug, reported]),
  );
  expect(shownLabels()).toEqual(["bug", "ready"]);
  expect(screen.queryByRole("alert")).toBeNull();
  await waitFor(() => expect(announced()).toBe("Added the label ready."));
});

it("applies several checks as one change once the picker closes", async () => {
  const docs: PullRequestLabel = { name: "docs", color: "0075ca", description: null };
  const { posts } = stubFetch(() =>
    json({ object: "list", data: [bug, docs, ready], has_more: false }),
  );
  renderLabels([bug]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  fireEvent.click(await screen.findByRole("option", { name: /^ready/ }));
  fireEvent.click(screen.getByRole("option", { name: /^docs/ }));
  fireEvent.click(screen.getByRole("option", { name: /^bug/ }));
  expect(screen.getByText("2 to add, 1 to remove · applied when you close")).toBeInTheDocument();
  expect(posts).toHaveLength(0);

  closePicker();
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: ["ready", "docs"], remove: ["bug"] });
  expect(await spoken()).toBe("Adding the labels ready and docs and removing the label bug…");
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_labels", labels: [docs, ready] })),
  );
  await waitFor(() => expect(shownLabels()).toEqual(["docs", "ready"]));
  await waitFor(() =>
    expect(announced()).toBe("Added the labels ready and docs and removed the label bug."),
  );
  expect(posts).toHaveLength(1);
});

it("sends nothing when the picker closes with its checks where they started", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
  renderLabels([bug]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  fireEvent.click(await screen.findByRole("option", { name: /^ready/ }));
  fireEvent.click(screen.getByRole("option", { name: /^ready/ }));
  expect(screen.queryByText(/applied when you close/)).toBeNull();
  closePicker();
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
  });
  expect(posts).toHaveLength(0);
  expect(shownLabels()).toEqual(["bug"]);
});

it("rolls back a refused edit, shows why, and refreshes the PR", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
  const client = renderLabels([bug, ready]);

  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  // The chip stays, struck, until GitHub answers.
  await waitFor(() => expect(shownLabels()).toEqual(["bug: Removing…", "ready"]));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: [], remove: ["bug"] });

  const detail = "The GitHub account on the host cannot edit this pull request's labels.";
  await act(async () => posts[0].respond(json({ detail }, 400)));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    `Couldn’t remove the label bug: ${detail}`,
  );
  expect(shownLabels()).toEqual(["bug", "ready"]);
  expect(client.getQueryState(INFO_KEY)?.isInvalidated).toBe(true);
});

it("sends chip removals one at a time in click order", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
  const client = renderLabels([bug, ready]);

  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  await waitFor(() => expect(shownLabels()).toEqual(["bug: Removing…", "ready"]));
  fireEvent.click(screen.getByRole("button", { name: "Remove label ready" }));
  await waitFor(() => expect(shownLabels()).toEqual(["bug: Removing…", "ready: Removing…"]));

  // The second removal waits until GitHub has answered the first.
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: [], remove: ["bug"] });
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_labels", labels: [ready] })),
  );
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].body).toEqual({ pr_url: PR_URL, add: [], remove: ["ready"] });
  await act(async () => posts[1].respond(json({ object: "session.github.pr_labels", labels: [] })));
  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.labels).toEqual([]),
  );
  expect(shownLabels()).toEqual([]);
});

it("keeps a refused edit's failure past the cache's cleanup, and forgets it once a retry lands", async () => {
  // Real time still runs; the cleanup timers can be skipped ahead.
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
  try {
    const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
    const client = renderLabels([bug]);
    fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    await act(async () => posts[0].respond(json({ detail: "Refused by GitHub." }, 400)));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refused by GitHub.");

    // Nothing shows it for an hour, well past an unwatched mutation's five minutes.
    cleanup();
    act(() => vi.advanceTimersByTime(60 * 60_000));
    render(
      <QueryClientProvider client={client}>
        <CachedLabels />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Refused by GitHub.");

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    await act(async () =>
      posts[1].respond(json({ object: "session.github.pr_labels", labels: [] })),
    );
    await waitFor(() => expect(shownLabels()).toEqual([]));
    // Only the change that landed stays, so kept changes don't pile up.
    const settled = client
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.state.status);
    expect(settled).toEqual(["success"]);
  } finally {
    vi.useRealTimers();
  }
});

it("keeps a refused edit's failure after its section remounts, until Retry goes through", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
  const client = renderLabels([bug]);
  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => posts[0].respond(json({ detail: "Refused by GitHub." }, 400)));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t remove the label bug: Refused by GitHub.",
  );
  // Said once, by the alert: the polite region doesn't repeat it.
  expect(await spoken()).not.toMatch(/Couldn’t/);

  // The PR was closed and opened again: the failure is still said, read from the cache.
  cleanup();
  render(
    <QueryClientProvider client={client}>
      <CachedLabels />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("Refused by GitHub.");

  // Retry is a fresh change, sent as the label alone.
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].body).toEqual({ pr_url: PR_URL, add: [], remove: ["bug"] });
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => posts[1].respond(json({ object: "session.github.pr_labels", labels: [] })));
  await waitFor(() => expect(shownLabels()).toEqual([]));
  expect(await spoken()).toBe("Removed the label bug.");
});

it("forgets repeated refusals with one Dismiss", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
  renderLabels([bug]);
  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => posts[0].respond(json({ detail: "First refusal." }, 400)));
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  await act(async () => posts[1].respond(json({ detail: "Second refusal." }, 400)));
  expect(await screen.findByRole("alert")).toHaveTextContent("Second refusal.");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  // The first refusal doesn't come back in its place.
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(shownLabels()).toEqual(["bug"]);
});

it("does not show one PR's failed edit under the next PR's labels", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const section = (prUrl: string, labels: PullRequestLabel[]) => (
    <QueryClientProvider client={client}>
      <PullRequestLabels conversationId="conv_1" prUrl={prUrl} labels={labels} editable />
    </QueryClientProvider>
  );
  const { rerender } = render(section(PR_URL, [bug]));

  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  await act(async () => posts[0].respond(json({ detail: "Refused by GitHub" }, 400)));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refused by GitHub");

  rerender(section("https://github.com/acme/app/pull/8", []));
  expect(screen.getByText("No labels.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each([
  [
    "a load failure",
    () =>
      json({ detail: "GitHub could not list this repository's labels. Refresh to retry." }, 400),
    "GitHub could not list this repository's labels. Refresh to retry.",
  ],
  [
    "an empty repository",
    () => json({ object: "list", data: [], has_more: false }),
    "This repository has no labels.",
  ],
  [
    "a truncated list",
    () => json({ object: "list", data: [bug], has_more: true }),
    "Showing the first 1 labels.",
  ],
])("the picker reports %s", async (_case, repoLabels, message) => {
  stubFetch(repoLabels);
  renderLabels([]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));

  expect(await screen.findByText(message)).toBeInTheDocument();
});

describe("pressing anywhere on the section", () => {
  const picker = () =>
    screen.queryByRole("textbox", { name: "Filter labels" }) ??
    screen.queryByPlaceholderText("Filter labels…");

  it("opens the picker from the title or the empty space, and a second press closes it", async () => {
    stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
    renderLabels([]);
    fireEvent.click(screen.getByRole("heading", { name: "Labels" }));
    await waitFor(() => expect(picker()).not.toBeNull());
    // Closing returns focus to the one keyboard stop, the trigger.
    fireEvent.keyDown(picker()!, { key: "Escape" });
    await waitFor(() => expect(picker()).toBeNull());
    expect(screen.getByRole("button", { name: "Edit labels" })).toHaveFocus();

    fireEvent.click(screen.getByText("No labels."));
    await waitFor(() => expect(picker()).not.toBeNull());
    const title = screen.getByRole("heading", { name: "Labels" });
    fireEvent.pointerDown(title);
    fireEvent.click(title);
    await waitFor(() => expect(picker()).toBeNull());
  });

  it("leaves a chip's remove button to remove the label, without opening the picker", async () => {
    const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
    renderLabels([bug, ready]);
    fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(picker()).toBeNull();
  });

  it("keeps one tab stop and the heading outside it", () => {
    stubFetch(() => json({ object: "list", data: [], has_more: false }));
    renderLabels([]);
    const section = screen.getByRole("heading", { name: "Labels" }).closest("section")!;
    expect(within(section).getAllByRole("button")).toHaveLength(1);
    expect(section.querySelector("button h3, button button")).toBeNull();
  });
});

it("does nothing on a press when the labels can't be edited", async () => {
  stubFetch(() => json({ object: "list", data: [], has_more: false }));
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <PullRequestLabels conversationId="conv_1" prUrl={PR_URL} labels={[bug]} editable={false} />
    </QueryClientProvider>,
  );
  const section = screen.getByRole("heading", { name: "Labels" }).closest("section")!;
  expect(section).not.toHaveClass("cursor-pointer");
  fireEvent.click(section);
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(screen.queryByPlaceholderText("Filter labels…")).toBeNull();
});

describe("opening from a row's Edit Labels…", () => {
  /** The section in each of `views`, as two pull request views showing the same PR do. */
  function renderIn(views: { id: string; active: boolean }[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (shown: typeof views) => (
      <QueryClientProvider client={client}>
        {shown.map((view) => (
          <PullRequestDestinationContext.Provider key={view.id} value={view}>
            <div data-testid={view.id}>
              <CachedLabels />
            </div>
          </PullRequestDestinationContext.Provider>
        ))}
      </QueryClientProvider>
    );
    const view = render(tree(views));
    return { rerender: (shown: typeof views) => view.rerender(tree(shown)) };
  }
  const trigger = (id: string) =>
    within(screen.getByTestId(id)).getByRole("button", { name: "Edit labels" });

  afterEach(() => resetControlRequestsForTests());

  it("opens the picker only in the view the row opens its PR in, once that view shows", async () => {
    stubFetch(() => json({ object: "list", data: [bug], has_more: false }));
    const views = renderIn([
      { id: "detail", active: false },
      { id: "sidebar", active: true },
    ]);
    act(() => requestPullRequestControl(PR_URL, "labels", { destination: "detail" }, null));
    // The sidebar shows the same PR, but the request isn't aimed at it.
    expect(trigger("sidebar")).toHaveAttribute("aria-expanded", "false");
    expect(trigger("detail")).toHaveAttribute("aria-expanded", "false");
    views.rerender([
      { id: "detail", active: true },
      { id: "sidebar", active: true },
    ]);
    await waitFor(() => expect(trigger("detail")).toHaveAttribute("aria-expanded", "true"));
    expect(trigger("sidebar")).toHaveAttribute("aria-expanded", "false");
  });

  it("reopens in the section that replaces one that went with the picker up, not once closed", async () => {
    stubFetch(() => json({ object: "list", data: [bug], has_more: false }));
    const views = renderIn([{ id: "detail", active: true }]);
    act(() => requestPullRequestControl(PR_URL, "labels", { destination: "detail" }, null));
    await waitFor(() => expect(trigger("detail")).toHaveAttribute("aria-expanded", "true"));
    // The PR finishes loading and its sections render afresh.
    views.rerender([]);
    views.rerender([{ id: "detail", active: true }]);
    await waitFor(() => expect(trigger("detail")).toHaveAttribute("aria-expanded", "true"));
    fireEvent.keyDown(screen.getByPlaceholderText("Filter labels…"), { key: "Escape" });
    await waitFor(() => expect(trigger("detail")).toHaveAttribute("aria-expanded", "false"));
    views.rerender([]);
    views.rerender([{ id: "detail", active: true }]);
    expect(trigger("detail")).toHaveAttribute("aria-expanded", "false");
  });
});
