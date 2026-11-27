import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MessageResponse } from "@/components/ai-elements/message";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import { GITHUB_MARKDOWN_REHYPE_PLUGINS } from "@/lib/githubMarkdownPlugins";

// The session's host, which looks up every PR here.
const host = vi.hoisted(() => ({
  serves: true,
  allowed: true,
  near: true,
  lookups: [] as { url: string; answer: (data: unknown) => void }[],
}));

vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_session: string, method: string) =>
    host.serves && method === "lookupPullRequest",
}));
vi.mock("@/lib/githubAutomaticReads", () => ({
  useAutomaticReadsAllowed: () => host.allowed,
  automaticReadsAllowed: () => host.allowed,
}));
vi.mock("@/lib/pullRequestReferences/nearViewport", () => ({
  useHasBeenNearViewport: () => host.near,
}));
vi.mock("@/lib/hostPullRequestReads", () => ({
  readPullRequestLookupOnHost: (_session: string, url: string) =>
    new Promise((resolve, reject) => {
      host.lookups.push({
        url,
        answer: (data) =>
          data === "missing" ? reject(new PullRequestApiError("Not found", 400)) : resolve(data),
      });
    }),
}));

const { PULL_REQUEST_MARKDOWN_COMPONENTS } =
  await import("@/newPullRequestPage/shell/PullRequestDetails");
const { PullRequestReferenceScope } = await import("./PullRequestReferenceScope");

const PAGE = "https://github.com/acme/app/pull/40";

function pr(url: string, state: string, title: string, isDraft = false) {
  const { repository, number } = parseCodeHostPullRequestUrl(url, ["git.example.com"])!;
  return { url, number, title, state, is_draft: isDraft, repository, updated_at: null };
}

const NO_SEEDS = {};

let queryClient: QueryClient;

function Page({
  body,
  page = PAGE,
  scope = NO_SEEDS,
}: {
  body: string;
  page?: string;
  scope?: Omit<ComponentProps<typeof PullRequestReferenceScope>, "pullRequestUrl" | "children">;
}): ReactNode {
  return (
    <QueryClientProvider client={queryClient}>
      <PullRequestReferenceScope pullRequestUrl={page} conversationId="s1" {...scope}>
        <MessageResponse
          rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
          components={PULL_REQUEST_MARKDOWN_COMPONENTS}
        >
          {body}
        </MessageResponse>
      </PullRequestReferenceScope>
    </QueryClientProvider>
  );
}

/** Waits until no lookup is running and the cache has told every reference. */
async function settled() {
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    });
  });
}

async function answer(url: string, data: unknown) {
  const lookup = host.lookups.find((entry) => entry.url === url);
  expect(lookup, url).toBeDefined();
  await act(async () => lookup!.answer(data));
}

beforeEach(() => {
  queryClient = new QueryClient();
  Object.assign(host, { serves: true, allowed: true, near: true, lookups: [] });
});

afterEach(async () => {
  cleanup();
  // Every lookup the shared queue sent settles, so the next case starts with all its slots.
  await act(async () => {
    for (const lookup of host.lookups) lookup.answer(null);
  });
  queryClient.clear();
});

describe("PR references in PR Markdown", () => {
  it("shows each reference's status once it's looked up, and leaves code and other links alone", async () => {
    const body = [
      "Stacked on #12, see acme/lib#3 and [the files](https://github.com/acme/app/pull/14/files).",
      "",
      "https://github.com/acme/app/pull/15 and [docs](https://example.com) and `#16`.",
    ].join("\n");
    const { container } = render(<Page body={body} />);
    await waitFor(() => expect(host.lookups).toHaveLength(4));

    // Before an answer, each is a plain link to its PR.
    expect(screen.getByRole("link", { name: "#12" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/12",
    );

    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "MERGED", "Add the parser"),
    );
    await answer(
      "https://github.com/acme/lib/pull/3",
      pr("https://github.com/acme/lib/pull/3", "OPEN", "Lib change", true),
    );
    await answer(
      "https://github.com/acme/app/pull/14",
      pr("https://github.com/acme/app/pull/14", "CLOSED", "Old try"),
    );
    await answer(
      "https://github.com/acme/app/pull/15",
      pr("https://github.com/acme/app/pull/15", "OPEN", "Next step"),
    );

    const merged = await screen.findByRole("link", { name: "#12, Merged: Add the parser" });
    expect(merged).toHaveAttribute("data-pull-request-state", "merged");
    expect(merged).toHaveTextContent(/^#12$/);
    expect(screen.getByRole("link", { name: "acme/lib#3, Draft: Lib change" })).toBeInTheDocument();
    // A link keeps its own text and where it goes; a bare one reads as its number.
    const files = screen.getByRole("link", { name: "the files, Closed: Old try" });
    expect(files).toHaveAttribute("href", "https://github.com/acme/app/pull/14/files");
    expect(screen.getByRole("link", { name: "#15, Open: Next step" })).toHaveTextContent(/^#15$/);
    expect(screen.getByRole("link", { name: "docs" })).not.toHaveAttribute(
      "data-pull-request-state",
    );
    expect(container.querySelector("code")).toHaveTextContent("#16");
  });

  it("keeps a focused reference's link, and its focus, as its status arrives", async () => {
    render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    const link = screen.getByRole("link", { name: "#12" });
    act(() => link.focus());
    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "OPEN", "Add the parser"),
    );
    expect(await screen.findByRole("link", { name: "#12, Open: Add the parser" })).toBe(link);
    expect(link).toHaveFocus();
  });

  it("never opens a link's card behind an open dialog, and closes it when one opens over it", async () => {
    const user = userEvent.setup();
    render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "OPEN", "Add the parser"),
    );
    const link = await screen.findByRole("link", { name: "#12, Open: Add the parser" });
    // A sheet over the page, as the PR sidebar opens on a narrow window.
    const sheet = document.createElement("div");
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("data-state", "open");
    document.body.append(sheet);
    await user.hover(link);
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 500);
      });
    });
    expect(screen.queryByTestId("pull-request-reference-card")).toBeNull();

    sheet.remove();
    await user.unhover(link);
    await user.hover(link);
    expect(await screen.findByTestId("pull-request-reference-card")).toBeInTheDocument();
    act(() => document.body.append(sheet));
    await waitFor(() => expect(screen.queryByTestId("pull-request-reference-card")).toBeNull());
    sheet.remove();
  });

  it("opens the PR's card on keyboard focus", async () => {
    const user = userEvent.setup();
    render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "OPEN", "Add the parser"),
    );
    await screen.findByRole("link", { name: "#12, Open: Add the parser" });

    await user.tab();

    const card = await screen.findByTestId("pull-request-reference-card");
    expect(within(card).getByText("Add the parser")).toBeInTheDocument();
    expect(within(card).getByLabelText("Pull request status: Open")).toBeInTheDocument();
  });

  it.each([
    ["OPEN", false, "Open", "text-status-green"],
    ["OPEN", true, "Draft", "text-muted-foreground"],
    ["MERGED", false, "Merged", "text-status-purple"],
    ["CLOSED", false, "Closed", "text-status-red"],
    ["QUEUED", false, "State unknown", "text-muted-foreground"],
  ])(
    "lays the card out quietly for a %s PR (draft: %s): where it lives, its title, then its state",
    async (state, isDraft, label, color) => {
      const user = userEvent.setup();
      render(<Page body="See #12." />);
      await waitFor(() => expect(host.lookups).toHaveLength(1));
      const title = "Give split terminal panes a hover title strip, so pane actions stop fighting";
      await answer("https://github.com/acme/app/pull/12", {
        ...pr("https://github.com/acme/app/pull/12", state, title, isDraft),
        updated_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
      });
      await user.tab();
      const card = await screen.findByTestId("pull-request-reference-card");
      const [meta] = card.children;
      expect(meta).toHaveTextContent(/^acme\/app·#12/);
      expect(within(card).getByRole("time")).toHaveAttribute(
        "title",
        expect.stringMatching(/^Updated /),
      );
      // A long title clamps to three lines.
      expect(within(card).getByTestId("pull-request-reference-card-title")).toHaveClass(
        "line-clamp-3",
      );
      const shown = within(card).getByLabelText(`Pull request status: ${label}`);
      expect(shown).toHaveTextContent(label);
      expect(shown).toHaveClass(color);
      // Nothing here offers an action, so the card's foot holds only the state.
      expect(within(card).queryByRole("button")).toBeNull();
    },
  );

  it("keeps a reference the read hasn't described a plain link with no card where nothing acts on it", async () => {
    const user = userEvent.setup();
    render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    await user.tab();
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 500);
      });
    });
    expect(screen.queryByTestId("pull-request-reference-card")).toBeNull();
  });

  it("keeps plain links without a lookup on a shell or browser that can't look PRs up", async () => {
    host.serves = false;
    render(<Page body="See #12 and https://github.com/acme/app/pull/13." />);

    expect(await screen.findByRole("link", { name: "#12" })).not.toHaveAttribute(
      "data-pull-request-state",
    );
    expect(screen.getByRole("link", { name: "#13" })).toBeInTheDocument();
    expect(host.lookups).toHaveLength(0);
  });

  it("looks nothing up until the reference is near the viewport or while reads are held", async () => {
    host.near = false;
    const { rerender } = render(<Page body="See #12." />);
    await screen.findByRole("link", { name: "#12" });
    host.near = true;
    host.allowed = false;
    // Each change rewrites the paragraph, so the reference renders again and reads the new state.
    rerender(<Page body="Now see #12." />);
    await screen.findByRole("link", { name: "#12" });
    expect(host.lookups).toHaveLength(0);

    host.allowed = true;
    rerender(<Page body="Then see #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
  });

  it("doesn't look a settled PR up again on a remount", async () => {
    const first = render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "OPEN", "Add the parser"),
    );
    first.unmount();

    render(<Page body="Again #12." />);

    await screen.findByRole("link", { name: "#12, Open: Add the parser" });
    expect(host.lookups).toHaveLength(1);
  });

  it("keeps the same number apart by host", async () => {
    render(<Page body="See #12." />);
    render(<Page body="See #12." page="https://git.example.com/acme/app/pull/40" />);
    await waitFor(() =>
      expect(host.lookups.map((entry) => entry.url).sort()).toEqual([
        "https://git.example.com/acme/app/pull/12",
        "https://github.com/acme/app/pull/12",
      ]),
    );
  });

  it("lets a lookup that waited send only if reads are still allowed, and resumes once they are", async () => {
    const body = "Five: #1 #2 #3 #4 #5.";
    const { rerender } = render(<Page body={body} />);
    await waitFor(() => expect(host.lookups).toHaveLength(4));

    // The reader's budget runs low while #5 waits its turn.
    host.allowed = false;
    rerender(<Page body="Five again: #1 #2 #3 #4 #5." />);
    await answer(
      "https://github.com/acme/app/pull/1",
      pr("https://github.com/acme/app/pull/1", "OPEN", "One"),
    );
    await act(async () => {});
    expect(host.lookups).toHaveLength(4);

    host.allowed = true;
    rerender(<Page body="Five once more: #1 #2 #3 #4 #5." />);
    await waitFor(() => expect(host.lookups).toHaveLength(5));
    expect(host.lookups[4]!.url).toBe("https://github.com/acme/app/pull/5");
  });

  it("drops the lookups a closed view was waiting on", async () => {
    const view = render(<Page body="Five: #1 #2 #3 #4 #5." />);
    await waitFor(() => expect(host.lookups).toHaveLength(4));

    view.unmount();
    await answer("https://github.com/acme/app/pull/1", null);
    await act(async () => {});

    expect(host.lookups).toHaveLength(4);
  });

  it("shows the view's freshest read of its own PR over an older one, and over a missing answer", async () => {
    const self = (state: string, title: string) => pr(PAGE, state, title);
    const now = Date.now();
    const { rerender } = render(
      <Page
        body="This is #40."
        scope={{ known: self("OPEN", "Page title"), knownReadAt: now - 3000 }}
      />,
    );
    await screen.findByRole("link", { name: "#40, Open: Page title" });

    rerender(
      <Page
        body="This is #40."
        scope={{ known: self("MERGED", "Updated title"), knownReadAt: now - 1000 }}
      />,
    );
    await screen.findByRole("link", { name: "#40, Merged: Updated title" });

    // An older snapshot never replaces it.
    rerender(
      <Page
        body="This is #40."
        scope={{ known: self("OPEN", "Page title"), knownReadAt: now - 2000 }}
      />,
    );
    await act(async () => {});
    expect(screen.getByRole("link", { name: "#40, Merged: Updated title" })).toBeInTheDocument();
    expect(host.lookups).toHaveLength(0);
  });

  it("promotes a PR a lookup found missing once the view itself reads it", async () => {
    const { rerender } = render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));
    await answer("https://github.com/acme/app/pull/12", null);
    expect(screen.getByRole("link", { name: "#12" })).not.toHaveAttribute(
      "data-pull-request-state",
    );

    rerender(
      <Page
        body="See #12."
        scope={{
          known: pr("https://github.com/acme/app/pull/12", "OPEN", "Found after all"),
          knownReadAt: Date.now(),
        }}
      />,
    );

    await screen.findByRole("link", { name: "#12, Open: Found after all" });
  });

  it("shows a stack's members as the view read them, even when they arrive after the references", async () => {
    const stack = (viewer: string) => ({
      viewer,
      members: [
        member("https://github.com/acme/app/pull/38", "MERGED", "Base of the stack"),
        member("https://github.com/acme/app/pull/39", "OPEN", "Middle of the stack"),
      ],
      missing: [],
      unread: [],
      readAt: Date.now(),
    });
    host.allowed = false;
    const { rerender } = render(<Page body="On #38 and #39." />);
    await screen.findByRole("link", { name: "#38" });

    rerender(<Page body="On #38 and #39." scope={{ stack: stack("mac-octo") }} />);

    await screen.findByRole("link", { name: "#38, Merged: Base of the stack" });
    expect(
      screen.getByRole("link", { name: "#39, Open: Middle of the stack" }),
    ).toBeInTheDocument();
    expect(host.lookups).toHaveLength(0);
  });

  it("leaves a link to a PR on another host as written, and never looks it up", async () => {
    const external = "https://github.com/acme/app/pull/12";
    render(<Page body={`See ${external}.`} page="https://git.example.com/acme/app/pull/40" />);

    const link = await screen.findByRole("link", { name: external });
    expect(link).toHaveAttribute("href", external);
    await act(async () => {});
    expect(host.lookups).toHaveLength(0);
  });

  it("keeps the view's newer read of its own PR over an older cached stack answer naming it", async () => {
    const now = Date.now();
    render(
      <Page
        body="This is #40."
        scope={{
          known: pr(PAGE, "MERGED", "New title"),
          knownReadAt: now - 1000,
          stack: stackOf([member(PAGE, "OPEN", "Old title")], now - 60_000),
        }}
      />,
    );

    await screen.findByRole("link", { name: "#40, Merged: New title" });
    expect(host.lookups).toHaveLength(0);
  });

  it("lets a stack read newer than the view's own read say where the PR stands now", async () => {
    const now = Date.now();
    render(
      <Page
        body="This is #40."
        scope={{
          known: pr(PAGE, "OPEN", "Old title"),
          knownReadAt: now - 60_000,
          stack: stackOf([member(PAGE, "MERGED", "New title")], now - 1000),
        }}
      />,
    );

    await screen.findByRole("link", { name: "#40, Merged: New title" });
  });

  it("seeds nothing from a stack that stands in for another read, as a list's", async () => {
    render(
      <Page
        body="On #38."
        scope={{
          stack: {
            ...stackOf([member("https://github.com/acme/app/pull/38", "MERGED", "Listed")], 0),
            readAt: undefined,
          },
        }}
      />,
    );

    await waitFor(() => expect(host.lookups).toHaveLength(1));
    expect(screen.queryByRole("link", { name: /Listed/ })).not.toBeInTheDocument();
  });

  it.each([
    ["found", pr("https://github.com/acme/app/pull/12", "OPEN", "Old title")],
    ["missing", null],
  ])("keeps a newer seed when an older lookup answers %s after it", async (_answer, late) => {
    const { rerender } = render(<Page body="See #12." />);
    await waitFor(() => expect(host.lookups).toHaveLength(1));

    // The view reads the PR itself after the lookup went out.
    const known = pr("https://github.com/acme/app/pull/12", "MERGED", "New title");
    rerender(<Page body="See #12." scope={{ known, knownReadAt: Date.now() + 1 }} />);
    await screen.findByRole("link", { name: "#12, Merged: New title" });

    await answer("https://github.com/acme/app/pull/12", late);
    await settled();

    expect(screen.getByRole("link", { name: "#12, Merged: New title" })).toBeInTheDocument();
  });

  it("lets a lookup sent after an old seed replace it", async () => {
    const old = Date.now() - 6 * 60_000;
    render(
      <Page
        body="See #12."
        scope={{
          known: pr("https://github.com/acme/app/pull/12", "OPEN", "Old title"),
          knownReadAt: old,
        }}
      />,
    );
    // Past its freshness, the seed is read again.
    await waitFor(() => expect(host.lookups).toHaveLength(1));

    await answer(
      "https://github.com/acme/app/pull/12",
      pr("https://github.com/acme/app/pull/12", "MERGED", "New title"),
    );

    await screen.findByRole("link", { name: "#12, Merged: New title" });
  });
});

function member(url: string, state: "OPEN" | "CLOSED" | "MERGED", title: string) {
  const { number } = parseCodeHostPullRequestUrl(url)!;
  return {
    url,
    number,
    title,
    state,
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: null,
    head_sha: null,
    base_ref: null,
    base_sha: null,
    head_repository: "acme/app",
    base_repository: "acme/app",
  };
}

function stackOf(members: ReturnType<typeof member>[], readAt: number) {
  return { viewer: "mac-octo", members, missing: [], unread: [], readAt };
}

describe("Mentions in PR Markdown", () => {
  it("links people and teams on the PR's own host, an Enterprise one too", () => {
    render(
      <Page
        body={"Thanks @octo, cc @acme/web. Mail octo@example.com, not `@octo`."}
        page="https://git.example.com/acme/app/pull/40"
      />,
    );

    expect(screen.getByRole("link", { name: "@octo" })).toHaveAttribute(
      "href",
      "https://git.example.com/octo",
    );
    expect(screen.getByRole("link", { name: "@acme/web" })).toHaveAttribute(
      "href",
      "https://git.example.com/orgs/acme/teams/web",
    );
    // The email address is GitHub's own mail link, never a profile.
    expect(screen.getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "https://git.example.com/octo",
      "https://git.example.com/orgs/acme/teams/web",
      "mailto:octo@example.com",
    ]);
  });

  it("keeps a mention as written outside a PR's view", () => {
    const { container } = render(
      <MessageResponse
        rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
        components={PULL_REQUEST_MARKDOWN_COMPONENTS}
      >
        Thanks @octo
      </MessageResponse>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.textContent).toBe("Thanks @octo");
  });
});
