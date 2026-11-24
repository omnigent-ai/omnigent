// PullRequestLabels against the real label hooks, with fetch stubbed: edits show
// before the host answers, run one at a time, settle to the labels it reports,
// and a refused edit rolls back with the host's message.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type { PullRequestInfo, PullRequestLabel } from "@/hooks/usePullRequests";
import { PullRequestLabels } from "./PullRequestLabels";

const PR_URL = "https://github.com/acme/app/pull/7";
const INFO_KEY = ["github-info", "conv_1", PR_URL];
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

function shownLabels(): string[] {
  const list = screen.queryByRole("list", { name: "Labels" });
  return list
    ? within(list)
        .getAllByRole("listitem")
        .map((item) => item.textContent ?? "")
    : [];
}

it("shows an edit at once and keeps the labels the host reports", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
  const client = renderLabels([bug]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  expect(await screen.findByRole("option", { name: /^bug/ })).toHaveAttribute(
    "data-checked",
    "true",
  );
  expect(screen.getByRole("option", { name: /^ready/ })).toHaveAttribute("data-checked", "false");
  fireEvent.click(screen.getByRole("option", { name: /^ready/ }));

  await waitFor(() => expect(shownLabels()).toEqual(["bug", "ready"]));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: ["ready"], remove: [] });

  const reported = { ...ready, description: "Reported by GitHub" };
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_labels", labels: [bug, reported] })),
  );

  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.labels).toEqual([bug, reported]),
  );
  expect(shownLabels()).toEqual(["bug", "ready"]);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("rolls back a refused edit, shows why, and refreshes the PR", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [], has_more: false }));
  const client = renderLabels([bug, ready]);

  fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
  await waitFor(() => expect(shownLabels()).toEqual(["ready"]));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: [], remove: ["bug"] });

  const detail = "The GitHub account on the host cannot edit this pull request's labels.";
  await act(async () => posts[0].respond(json({ detail }, 400)));

  expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  expect(shownLabels()).toEqual(["bug", "ready"]);
  expect(client.getQueryState(INFO_KEY)?.isInvalidated).toBe(true);
});

it("sends edits one at a time in click order, so a quick add then remove ends removed", async () => {
  const { posts } = stubFetch(() => json({ object: "list", data: [bug, ready], has_more: false }));
  const client = renderLabels([bug]);

  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  fireEvent.click(await screen.findByRole("option", { name: /^ready/ }));
  await waitFor(() => expect(shownLabels()).toEqual(["bug", "ready"]));
  fireEvent.click(screen.getByRole("option", { name: /^ready/ }));
  await waitFor(() => expect(shownLabels()).toEqual(["bug"]));

  // The removal waits until GitHub has answered the add.
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].body).toEqual({ pr_url: PR_URL, add: ["ready"], remove: [] });
  await act(async () =>
    posts[0].respond(json({ object: "session.github.pr_labels", labels: [bug, ready] })),
  );
  expect(shownLabels()).toEqual(["bug"]);

  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1].body).toEqual({ pr_url: PR_URL, add: [], remove: ["ready"] });
  await act(async () =>
    posts[1].respond(json({ object: "session.github.pr_labels", labels: [bug] })),
  );
  await waitFor(() =>
    expect(client.getQueryData<PullRequestInfo>(INFO_KEY)?.pr?.labels).toEqual([bug]),
  );
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
