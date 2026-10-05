// Tests for PullRequestPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestAuth,
  PullRequestChangedFile,
  PullRequestComment,
  PullRequestDiffResponse,
  PullRequestInfo,
} from "@/hooks/usePullRequests";

const state = vi.hoisted(() => ({
  info: null as {
    data?: PullRequestInfo;
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  changes: null as {
    data?: {
      available: boolean;
      data: PullRequestChangedFile[];
      has_more?: boolean;
      warning?: string;
    };
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  // Per-file diffs the stubbed parsePatchFiles yields (name + optional
  // rename fields), so tests can exercise renamed/pure-rename rendering.
  parsedFiles: [] as {
    name: string;
    prevName?: string;
    type?: string;
    unifiedLineCount?: number;
  }[],
  // The whole-PR diff response; the panel parses its patch into per-file diffs.
  diff: null as PullRequestDiffResponse | null,
  diffError: null as Error | null,
}));

vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  // The panel reads every info payload through the real normalizer.
  normalizePullRequestInfo: (await importOriginal<typeof UsePullRequestsModule>())
    .normalizePullRequestInfo,
  usePullRequestInfo: vi.fn(() => state.info),
  usePullRequestChangedFiles: vi.fn(() => state.changes),
  usePullRequestDiff: () => ({
    data: state.diff,
    isLoading: false,
    error: state.diffError,
    isFetching: false,
  }),
  fetchPullRequestFileContents: async () => ({ before: "old", after: "new" }),
  // The account selector (shown in the repo-unresolved empty state) calls this;
  // stub the mutation shape it reads.
  useUpdateSessionPr: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useSetPullRequestPreference: () => ({
    mutate: () => {},
    isPending: false,
    isError: false,
    error: null,
  }),
}));

// The diff rendering (@pierre/diffs) is exercised by the library itself; here
// we only assert a section renders one diff per parsed file. parsePatchFiles is
// stubbed to yield the files configured on `state` (name + optional rename
// metadata), matching the whole-PR patch.
vi.mock("@pierre/diffs", () => ({
  parsePatchFiles: () => [{ files: state.parsedFiles }],
}));
vi.mock("@pierre/diffs/react", () => ({
  FileDiff: ({ fileDiff }: { fileDiff: { name: string } }) => (
    <div data-testid="diff" data-path={fileDiff.name} />
  ),
}));
// The resolved theme mode drives @pierre/diffs' themeType.
vi.mock("@/components/theme/useResolvedThemeMode", () => ({
  useResolvedThemeMode: () => "light",
}));
// The Summary tab renders markdown via MessageResponse (Streamdown); stub it to
// a passthrough so tests assert the text without the real renderer.
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => (
    <div data-testid="markdown">{children}</div>
  ),
}));

import { usePullRequestInfo, usePullRequestChangedFiles } from "@/hooks/usePullRequests";

import {
  PullRequestPanel,
  derivePullRequestPanelState,
  LARGE_DIFF_THRESHOLD,
} from "./PullRequestPanel";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

function file(
  path: string,
  status: PullRequestChangedFile["status"],
  adds = 1,
  dels = 0,
): PullRequestChangedFile {
  return {
    path,
    name: path.split("/").pop() ?? path,
    status,
    bytes: null,
    modified_at: null,
    lines_added: adds,
    lines_removed: dels,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<PullRequestPanel conversationId="conv_1" />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

/** Render, then switch to the Changes tab — Summary is the default, so the
 *  diff view (and its toolbar) only exists after activating Changes. Radix
 *  tabs select on pointer-down, so mouseDown (not click) flips the tab. */
function renderChanges() {
  const r = renderPanel();
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
  return r;
}

/** Silences console.error for the current test and returns a reader for the React
 *  duplicate- and missing-key warnings logged so far. */
function watchKeyWarnings(): () => string[] {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  onTestFinished(() => errors.mockRestore());
  return () =>
    errors.mock.calls
      .map(([message]) => String(message))
      .filter((message) => /same key|unique "key"/.test(message));
}

/** The markdown body of each comment card, in rendered order. The fixtures used
 *  with it have no PR description, so every markdown block is a comment. */
function commentBodies(): (string | null)[] {
  return screen.getAllByTestId("markdown").map((el) => el.textContent);
}

let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // The diff-layout toggle seeds from persisted prefs; start each test clean.
  window.localStorage.clear();
  // Fire the observer callback immediately on observe so lazy sections mount.
  class IO {
    private cb: IntersectionObserverCallback;
    constructor(cb: IntersectionObserverCallback) {
      this.cb = cb;
    }
    observe(el: Element) {
      this.cb(
        [{ isIntersecting: true, target: el } as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", IO);
  scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView as unknown as Element["scrollIntoView"];

  state.info = {
    data: {
      object: "session.github.info",
      available: true,
      gh_available: true,
      authenticated: true,
      branch: "test/pr-view",
      base_ref: "main",
      repo: { name_with_owner: "acme/app" },
      pr: {
        number: 6000,
        title: "chore: dummy PR",
        state: "OPEN",
        url: "https://example.com/pr/6000",
        is_draft: false,
        author: "dev",
        base_ref: "main",
        head_ref: "test/pr-view",
        checks: {
          passing: 66,
          failing: 2,
          pending: 0,
          total: 68,
          runs: [
            { name: "unit tests", bucket: "passing", url: null },
            { name: "e2e", bucket: "failing", url: null },
          ],
        },
      },
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
  state.changes = {
    data: {
      available: true,
      data: [file("hello.py", "created"), file("src/app.ts", "modified", 3, 1)],
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
  // Default parsed diffs mirror the two changed files above.
  state.parsedFiles = [{ name: "hello.py" }, { name: "src/app.ts" }];
  state.diff = { object: "session.github.pr_diff", patch: "PATCH" };
  state.diffError = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("PullRequestPanel", () => {
  it("shows the PR title in the header and CI check pills on the Summary tab", async () => {
    renderPanel();
    const heading = screen.getByRole("heading", { name: "GitHub" });
    expect(heading).toHaveAttribute("title", "GitHub");
    expect(screen.getByText("GitHub")).toHaveClass("sr-only");
    expect(heading.parentElement).toHaveClass("h-11");
    // Title + number live in the shared header (both tabs).
    expect(await screen.findByText("chore: dummy PR")).toBeInTheDocument();
    expect(screen.getByText("#6000")).toBeInTheDocument();
    // CI checks render on the Summary tab (the default) as labeled pills. A
    // zero bucket (pending) renders no pill.
    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByText(/66\s*passed/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*failed/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/)).toBeNull();
  });

  it.each([
    ["OPEN", "Open", "text-green-700"],
    ["CLOSED", "Closed", "text-red-700"],
    ["MERGED", "Merged", "text-purple-700"],
  ])("shows a %s status pill beside the PR title", (stateName, label, tone) => {
    state.info!.data!.pr!.state = stateName;
    renderPanel();

    const pill = screen.getByLabelText(`Pull request status: ${label}`);
    expect(pill).toHaveTextContent(label);
    expect(pill).toHaveClass(tone, "h-5", "rounded-full", "border", "text-xs");
    expect(pill.parentElement).toHaveClass("flex-nowrap");
    expect(screen.getByRole("link", { name: /chore: dummy PR/ })).not.toHaveClass("flex-1");
  });

  it("lands on the Summary tab, showing the PR description and comments", async () => {
    state.info!.data!.pr!.body = "## Overview\nThis PR does the thing.";
    state.info!.data!.pr!.comments = [
      {
        author: "octocat",
        body: "Looks good to me!",
        created_at: "2026-09-05T07:32:02Z",
        url: "https://example.com/pr/6000#c1",
      },
    ];
    renderPanel();
    // Summary is the default — the diff sections aren't mounted yet.
    expect(screen.queryByTestId("diff")).toBeNull();
    expect(await screen.findByText(/This PR does the thing\./)).toBeInTheDocument();
    expect(screen.getByText("Comments (1)")).toBeInTheDocument();
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(screen.getByText("Looks good to me!")).toBeInTheDocument();
  });

  it("renders GitHub comments in order, including ones without a URL or time", () => {
    const keyWarnings = watchKeyWarnings();
    state.info!.data!.pr!.comments = [
      {
        author: "octocat",
        body: "First",
        created_at: "2026-09-05T07:32:02Z",
        url: "https://example.com/pr/6000#c1",
      },
      {
        author: "octocat",
        body: "Second",
        created_at: "2026-09-05T07:32:02Z",
        url: "https://example.com/pr/6000#c2",
      },
      { author: null, body: "Third", created_at: null, url: null },
      { author: null, body: "Fourth", created_at: null, url: null },
    ];
    renderPanel();

    expect(screen.getByText("Comments (4)")).toBeInTheDocument();
    expect(commentBodies()).toEqual(["First", "Second", "Third", "Fourth"]);
    expect(keyWarnings()).toEqual([]);
  });

  it("shows Summary empty states when the PR has no body or comments", () => {
    // The default fixture carries neither a body nor comments.
    renderPanel();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
    expect(screen.getByText("No comments yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("marks incomplete checks and comments without claiming they are empty", () => {
    state.info!.data!.pr!.checks = {
      passing: 0,
      failing: 0,
      pending: 0,
      total: 0,
      runs: [],
      partial: true,
    };
    state.info!.data!.pr!.comments_partial = true;
    renderPanel();

    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByText(/Some checks are unavailable/)).toBeInTheDocument();
    expect(screen.getByText("Some comments are unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("No comments yet.")).not.toBeInTheDocument();
  });

  it("retains loaded comments while indicating that more could not be fetched", () => {
    state.info!.data!.pr!.comments_partial = true;
    state.info!.data!.pr!.comments = [
      { author: "reviewer", body: "Loaded comment", created_at: null, url: null },
    ];
    renderPanel();

    expect(screen.getByText("Comments (1+)")).toBeInTheDocument();
    expect(screen.getByText("Loaded comment")).toBeInTheDocument();
    expect(screen.getByText("Some comments are unavailable.")).toBeInTheDocument();
  });

  it("shows lookup warnings instead of reporting that no request exists", () => {
    state.info!.data!.pr = null;
    state.info!.data!.warnings = ["Request lookup failed. Try again shortly."];
    renderPanel();

    expect(screen.getByRole("status")).toHaveTextContent(
      "Request lookup failed. Try again shortly.",
    );
    expect(screen.queryByText(/No open PR for/)).not.toBeInTheDocument();
  });

  it("reveals the stacked diff after switching to the Changes tab", async () => {
    renderPanel();
    expect(screen.queryByTestId("diff")).toBeNull();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    const diffs = await screen.findAllByTestId("diff");
    expect(diffs.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py", "src/app.ts"]);
    // Checks live on the Summary tab, so they're gone once Changes is active.
    expect(screen.queryByText("Checks")).toBeNull();
  });

  it("stacks a diff section per changed file", async () => {
    renderChanges();
    const diffs = await screen.findAllByTestId("diff");
    expect(diffs.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py", "src/app.ts"]);
  });

  it("jumps to a file's section when its sidebar row is clicked", async () => {
    renderChanges();
    await screen.findAllByTestId("diff");
    // Both the sidebar row and the section header are buttons matching the
    // name; the sidebar row (which scrolls) is first in the DOM.
    const row = screen.getAllByRole("button", { name: /app\.ts/ })[0];
    fireEvent.click(row);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("collapses a file's diff when its section header is clicked", async () => {
    renderChanges();
    expect(await screen.findAllByTestId("diff")).toHaveLength(2);
    // The section header carries aria-expanded; the sidebar row doesn't.
    const header = screen.getByRole("button", { name: /app\.ts/, expanded: true });
    fireEvent.click(header);
    // Only the other file's diff remains rendered.
    const remaining = screen.getAllByTestId("diff");
    expect(remaining.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py"]);
  });

  it("collapses and expands every diff from the toolbar", async () => {
    renderChanges();
    expect(await screen.findAllByTestId("diff")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Collapse all diffs" }));
    expect(screen.queryAllByTestId("diff")).toHaveLength(0);
    // The same button now offers the inverse action.
    fireEvent.click(screen.getByRole("button", { name: "Expand all diffs" }));
    expect(screen.getAllByTestId("diff")).toHaveLength(2);
  });

  it("hides and shows the file sidebar from the toolbar", async () => {
    renderChanges();
    await screen.findAllByTestId("diff");
    // hello.py appears as a sidebar jump row and as a section header.
    expect(screen.getAllByRole("button", { name: /hello\.py/ })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Hide file list" }));
    // Sidebar gone → only the section header remains.
    expect(screen.getAllByRole("button", { name: /hello\.py/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Show file list" }));
    expect(screen.getAllByRole("button", { name: /hello\.py/ })).toHaveLength(2);
  });

  it("toggles the diff layout between unified and split", async () => {
    renderChanges();
    await screen.findAllByTestId("diff");
    // Defaults to unified, so the toggle offers split; clicking flips its label.
    fireEvent.click(screen.getByRole("button", { name: "Switch to split view" }));
    expect(screen.getByRole("button", { name: "Switch to unified view" })).toBeInTheDocument();
  });

  it("groups the sidebar into a folder tree, compacting single-child chains", async () => {
    state.changes = {
      data: {
        available: true,
        data: [
          file("omnigent/runner/app.py", "modified"),
          file("omnigent/runner/util.py", "created"),
        ],
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderChanges();
    // The lone omnigent → runner chain collapses to a single "omnigent/runner"
    // folder row (exact name; the diff section headers carry the full path).
    expect(await screen.findByRole("button", { name: "omnigent/runner" })).toBeInTheDocument();
    // Each file shows as a leaf keyed by its basename.
    expect(screen.getAllByRole("button", { name: /app\.py/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: /util\.py/ }).length).toBeGreaterThan(0);
  });

  it("collapses a folder to hide its files in the sidebar tree", async () => {
    state.changes = {
      data: {
        available: true,
        data: [file("omnigent/runner/app.py", "modified")],
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderChanges();
    const folder = await screen.findByRole("button", { name: "omnigent/runner" });
    // Before collapse: the sidebar leaf + the diff section header both match.
    expect(screen.getAllByRole("button", { name: /app\.py/ })).toHaveLength(2);
    fireEvent.click(folder);
    // After collapse: only the diff section header remains (sidebar leaf gone).
    expect(screen.getAllByRole("button", { name: /app\.py/ })).toHaveLength(1);
  });

  it("shows a pure rename as a note with an old → new header", async () => {
    state.changes = {
      data: { available: true, data: [file("omnigent/new_name.py", "renamed")] },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    state.parsedFiles = [
      { name: "omnigent/new_name.py", prevName: "omnigent/old_name.py", type: "rename-pure" },
    ];
    renderChanges();
    // No diff body for a 100%-similarity rename — a note instead.
    expect(await screen.findByText("File renamed without changes.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
    // The section header reads old → new.
    expect(
      screen.getByRole("button", {
        name: /omnigent\/old_name\.py\s*→\s*omnigent\/new_name\.py/,
      }),
    ).toBeInTheDocument();
  });

  it("renders a non-git workspace message without a PR body", () => {
    state.info = {
      data: { object: "session.github.info", available: false, reason: "not_a_git_repo" },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("Not a git repository")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("prompts to update the host when it predates the GitHub route", () => {
    // fetchPullRequestInfo synthesizes this payload with no provider.
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "host_outdated",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("Update your host to use the Pull Requests tab")).toBeInTheDocument();
    expect(screen.getByText(/0\.13\.0 or later/)).toBeInTheDocument();
    expect(screen.queryByText(/GitHub/)).toBeNull();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("prompts to install the GitHub CLI when gh is missing", () => {
    state.info!.data!.gh_available = false;
    renderPanel();
    expect(screen.getByText("GitHub CLI not found")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("prompts to check gh auth when the upstream repo can't be resolved", () => {
    // Signed in, but `gh repo view` failed → no repo resolved.
    state.info!.data!.authenticated = true;
    state.info!.data!.repo = null;
    renderPanel();
    expect(screen.getByText("Can’t reach the upstream repo")).toBeInTheDocument();
    expect(screen.getByText(/gh auth status/)).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it.each([1, 2])("offers the stored PR in the empty state with %i GitHub accounts", (count) => {
    const url = "https://github.com/acme/app/pull/6000";
    const info = state.info!.data!;
    info.pr = null;
    info.repo = null;
    info.tracking_available = true;
    info.selected_pr_url = url;
    info.prs = [
      { url, host: "github.com", repository: "acme/app", number: 6000, relationship: "created" },
    ];
    info.accounts = ["personal", "work"].slice(0, count).map((login) => ({
      login,
      active: login === "personal",
      state: "success",
      host: "github.com",
    }));
    info.selected_account = "personal";
    renderPanel();
    const emptyState = screen.getByText("Can’t reach the upstream repo").parentElement;
    const link = screen.getByRole("link", { name: "Open the PR on GitHub" });
    expect(emptyState).toContainElement(link);
    expect(emptyState).toContainElement(screen.getByText("or", { exact: true }));
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    const account = screen.queryByRole("combobox", { name: "GitHub account" });
    if (count > 1) {
      expect(emptyState).toContainElement(account);
      expect(
        account!.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    } else {
      expect(account).toBeNull();
    }
  });

  it("hides the account selector when the provider can't switch accounts", () => {
    const info = state.info!.data!;
    info.repo = null;
    info.accounts = ["personal", "work"].map((login) => ({
      login,
      active: login === "personal",
      state: "success",
      host: "github.com",
    }));
    info.selected_account = "personal";
    info.capabilities = {
      account_switching: false,
      base_remote_selection: true,
      line_counts: true,
      linked_pr_diff: true,
    };
    renderPanel();
    expect(screen.getByText("Can’t reach the upstream repo")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
  });

  it("names the remote host when no provider supports the remote", () => {
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        remote_host: "git.example.com",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("No supported remote")).toBeInTheDocument();
    expect(screen.getByText("git.example.com")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
    // No provider serves the workspace, so the header names none.
    const heading = screen.getByRole("heading", { name: "Pull Requests" });
    expect(heading.querySelector("svg")).toHaveClass("lucide-git-pull-request");
    expect(screen.queryByRole("heading", { name: "GitHub" })).toBeNull();
  });

  it("tells a GitHub Enterprise user to sign in to the remote's host", () => {
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        remote_host: "git.example.com",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    // A second line under the sentence about the host; the command is set as code.
    const hint = screen.getByText(/which isn’t a supported/);
    const note = screen.getByText(/For a GitHub Enterprise host, run/);
    expect(note).not.toBe(hint);
    expect(hint.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(note).toHaveTextContent(
      "For a GitHub Enterprise host, run gh auth login --hostname git.example.com on the host.",
    );
    expect(screen.getByText("gh auth login --hostname git.example.com")).toHaveClass("font-mono");
  });

  it("leaves out the sign-in line when the remote's host is unknown", () => {
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(
      screen.getByText("This workspace’s remote isn’t on a supported git provider."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/GitHub Enterprise/)).toBeNull();
    expect(screen.queryByText(/gh auth login/)).toBeNull();
  });

  describe("header when no provider is known", () => {
    const neutralHeading = () => {
      const heading = screen.getByRole("heading", { name: "Pull Requests" });
      expect(heading.querySelector("svg")).toHaveClass("lucide-git-pull-request");
      expect(heading.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(screen.queryByText(/GitHub/)).toBeNull();
    };

    it("is neutral while the first load runs", () => {
      state.info = { isLoading: true, error: null, isFetching: true };
      renderPanel();
      expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
      neutralHeading();
    });

    it("is neutral when the first load fails", () => {
      state.info = { isLoading: false, error: new Error("boom"), isFetching: false };
      renderPanel();
      expect(screen.getByText("Couldn’t load pull request info: boom")).toBeInTheDocument();
      neutralHeading();
    });

    it("is neutral while the runner is offline", () => {
      state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
      renderPanel();
      expect(screen.getByText(/The agent is asleep/)).toBeInTheDocument();
      neutralHeading();
    });

    it("is neutral when the host is outdated", () => {
      state.info = {
        data: {
          object: "session.github.info",
          available: false,
          reason: "host_outdated",
          provider: null,
        },
        isLoading: false,
        error: null,
        isFetching: false,
      };
      renderPanel();
      neutralHeading();
    });

    it("says pull requests are unavailable without naming a provider", () => {
      state.info = {
        data: {
          object: "session.github.info",
          available: false,
          reason: "no_os_env",
          provider: null,
        },
        isLoading: false,
        error: null,
        isFetching: false,
      };
      renderPanel();
      expect(screen.getByText("Pull requests aren’t available")).toBeInTheDocument();
      expect(screen.getByText(/no pull request information/)).toBeInTheDocument();
      neutralHeading();
    });

    it.each([undefined, null, "github", "example_forge"])(
      "is neutral in a non-git workspace when the host's provider is %j",
      (provider) => {
        state.info = {
          data: {
            object: "session.github.info",
            available: false,
            reason: "not_a_git_repo",
            provider,
          },
          isLoading: false,
          error: null,
          isFetching: false,
        };
        renderPanel();
        expect(screen.getByText("Not a git repository")).toBeInTheDocument();
        neutralHeading();
      },
    );

    it("is neutral above the PR picker in a non-git workspace", () => {
      const url = "https://github.com/acme/app/pull/6000";
      state.info = {
        data: {
          object: "session.github.info",
          available: false,
          reason: "not_a_git_repo",
          tracking_available: true,
          selected_pr_url: url,
          prs: [
            {
              url,
              host: "github.com",
              repository: "acme/app",
              number: 6000,
              relationship: "created",
            },
          ],
        },
        isLoading: false,
        error: null,
        isFetching: false,
      };
      renderPanel();
      const heading = screen.getByRole("heading", { name: "Pull Requests" });
      expect(heading.querySelector("svg")).toHaveClass("lucide-git-pull-request");
      expect(screen.queryByRole("heading", { name: "GitHub" })).toBeNull();
      // The picker names the PR in its own provider's style; the header does not.
      expect(screen.getByRole("combobox", { name: "Session pull request" })).toHaveTextContent(
        "acme/app #6000",
      );
    });

    it("keeps naming GitHub for a host that omits the provider", () => {
      // The default fixture is a ready payload with no `provider`.
      renderPanel();
      const heading = screen.getByRole("heading", { name: "GitHub" });
      expect(heading.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(screen.queryByRole("heading", { name: "Pull Requests" })).toBeNull();
    });

    it("keeps the known provider in the header while a PR switch loads", () => {
      const url = "https://forge.example.test/contoso/web/_git/app/pullrequest/7";
      state.info!.data = {
        ...state.info!.data!,
        provider: "example_forge",
        provider_display: FORGE_DISPLAY,
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          {
            url,
            host: "forge.example.test",
            repository: "contoso/web/app",
            number: 7,
            relationship: "created",
            provider: "example_forge",
            provider_display: FORGE_DISPLAY,
          },
        ],
      };
      const { rerender } = renderPanel();
      expect(screen.getByRole("heading", { name: "Example Forge" })).toBeInTheDocument();
      state.info = { isLoading: true, error: null, isFetching: true };
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Example Forge" })).toBeInTheDocument();
    });
  });

  describe("with a provider the panel has no copy for", () => {
    const gitlab = (auth: Partial<PullRequestAuth>): PullRequestInfo => ({
      object: "session.github.info",
      available: true,
      provider: "gitlab",
      auth: {
        authenticated: true,
        hint: null,
        cli: { name: "glab", available: true },
        accounts: [
          { login: "personal", active: true, state: "success", host: "gitlab.com" },
          { login: "work", active: false, state: "success", host: "gitlab.com" },
        ],
        selected_account: "personal",
        ...auth,
      },
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: true,
        linked_pr_diff: false,
      },
      branch: "test/pr-view",
      repo: null,
      pr: null,
    });

    it("uses the generic label and names the missing CLI", () => {
      state.info!.data = gitlab({ authenticated: false, cli: { name: "glab", available: false } });
      renderPanel();
      expect(screen.getByRole("heading", { name: "gitlab" })).toBeInTheDocument();
      expect(screen.getByText("gitlab CLI not found")).toBeInTheDocument();
      expect(screen.getByText("glab")).toHaveClass("font-mono");
      expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
      expect(screen.queryByText(/GitHub/)).toBeNull();
    });

    it("shows the host's sign-in hint when the repo can't be reached", () => {
      state.info!.data = gitlab({
        authenticated: false,
        hint: "Run `glab auth login` on the host.",
      });
      renderPanel();
      expect(screen.getByText("Can’t reach the upstream repo")).toBeInTheDocument();
      expect(screen.getByText("glab auth login")).toHaveClass("font-mono");
      expect(screen.queryByText(/gh auth status/)).toBeNull();
      expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
    });
  });

  describe("with Example Forge", () => {
    const prUrl = "https://forge.example.test/contoso/web/_git/app/pullrequest/7";
    const auth = (over: Partial<PullRequestAuth> = {}): PullRequestAuth => ({
      authenticated: true,
      hint: "Run `forge login` on the host, or set `FORGE_TOKEN`.",
      cli: { name: "forge", available: true },
      accounts: null,
      selected_account: null,
      ...over,
    });
    const forge = (over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
      object: "session.github.info",
      available: true,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      auth: auth(),
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: false,
        linked_pr_diff: false,
      },
      branch: "feat/widget",
      base_ref: "main",
      repo: { name_with_owner: "contoso/web/app" },
      pr: {
        number: 7,
        title: "Add the widget",
        state: "OPEN",
        url: prUrl,
        is_draft: false,
        author: "dev",
        base_ref: "main",
        head_ref: "feat/widget",
        checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
      },
      ...over,
    });
    const association = (over: Partial<PullRequestAssociation> = {}): PullRequestAssociation => ({
      url: prUrl,
      host: "forge.example.test",
      repository: "contoso/web/app",
      number: 7,
      relationship: "created",
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      ...over,
    });

    it("shows the provider, the repo, and a !-prefixed PR with no account selector", () => {
      state.info!.data = forge();
      renderPanel();
      const heading = screen.getByRole("heading", { name: "Example Forge" });
      expect(heading.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(screen.getByText(/contoso\/web\/app/)).toBeInTheDocument();
      expect(screen.getByText("!7")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Add the widget/ })).toHaveAttribute("href", prUrl);
      expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
      expect(screen.queryByText(/GitHub/)).toBeNull();
    });

    it("labels linked PRs with a !, leaving out forge.example.test but not other hosts", async () => {
      const other = "https://other.example.test/fabrikam/api/_git/svc/pullrequest/12";
      state.info!.data = forge({
        tracking_available: true,
        selected_pr_url: prUrl,
        prs: [
          association({ title: "Add the widget" }),
          association({
            url: other,
            host: "other.example.test",
            repository: "fabrikam/api/svc",
            number: 12,
            relationship: "inferred",
          }),
        ],
      });
      renderPanel();
      fireEvent.click(screen.getByRole("button", { name: "Link a PR" }));
      expect(screen.getByRole("textbox", { name: "Pull request URL" })).toHaveAttribute(
        "placeholder",
        "Pull request URL",
      );
      const picker = screen.getByRole("combobox", { name: "Session pull request" });
      expect(picker).toHaveTextContent("contoso/web/app !7 — Add the widget");
      fireEvent.click(picker);
      expect(
        screen.getByRole("option", {
          name: "other.example.test/fabrikam/api/svc !12 (from branch)",
        }),
      ).toBeVisible();
    });

    it("goes straight to the PR when signed in without the Example Forge CLI", () => {
      state.info!.data = forge({ auth: auth({ cli: { name: "forge", available: false } }) });
      renderPanel();
      expect(screen.getByText("Add the widget")).toBeInTheDocument();
      expect(screen.queryByText("Example Forge CLI not found")).toBeNull();
    });

    it("prompts to install the Example Forge CLI, or set a token, when signed out without it", () => {
      state.info!.data = forge({
        auth: auth({ authenticated: false, cli: { name: "forge", available: false } }),
      });
      renderPanel();
      expect(screen.getByText("Example Forge CLI not found")).toBeInTheDocument();
      expect(screen.getByText("forge")).toHaveClass("font-mono");
      expect(screen.getByText("forge login")).toHaveClass("font-mono");
      expect(screen.getByText("FORGE_TOKEN")).toHaveClass("font-mono");
      expect(screen.queryByTestId("diff")).toBeNull();
    });

    it("shows the Example Forge sign-in hint when the repo can't be reached", () => {
      state.info!.data = forge({ auth: auth({ authenticated: false }), repo: null, pr: null });
      renderPanel();
      expect(screen.getByText("Can’t reach the upstream repo")).toBeInTheDocument();
      expect(screen.getByText("forge login")).toHaveClass("font-mono");
      expect(screen.getByText("FORGE_TOKEN")).toHaveClass("font-mono");
      expect(screen.queryByText(/gh auth status/)).toBeNull();
      expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
    });

    it("links the stored PR in its own provider's name when no provider serves the remote", () => {
      state.info!.data = {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        remote_host: "gitlab.com",
        provider: null,
        tracking_available: true,
        selected_pr_url: prUrl,
        prs: [association()],
      };
      renderPanel();
      expect(screen.getByText("No supported remote")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Open the PR on Example Forge" })).toHaveAttribute(
        "href",
        prUrl,
      );
    });

    describe("comment and check lists", () => {
      // Every comment in one discussion thread carries the thread's URL.
      const threadUrl = `${prUrl}?discussionId=3`;
      const comment = (over: Partial<PullRequestComment>): PullRequestComment => ({
        author: "Ada Lovelace",
        author_id: "u-ada",
        body: "",
        created_at: "2026-09-05T07:32:02.100Z",
        url: threadUrl,
        ...over,
      });
      const withPr = (over: Partial<PullRequest>): PullRequestInfo =>
        forge({ pr: { ...forge().pr!, ...over } });

      it("renders every comment of a thread whose replies share one URL", () => {
        const keyWarnings = watchKeyWarnings();
        state.info!.data = withPr({
          comments: [
            comment({ body: "Why not reuse the helper?" }),
            comment({
              author: "Grace Hopper",
              author_id: "u-grace",
              body: "It predates the helper.",
              created_at: "2026-09-05T08:01:40.250Z",
            }),
            comment({ body: "Fair, resolving this.", created_at: "2026-09-05T08:15:09.000Z" }),
          ],
        });
        renderPanel();

        expect(screen.getByText("Comments (3)")).toBeInTheDocument();
        expect(commentBodies()).toEqual([
          "Why not reuse the helper?",
          "It predates the helper.",
          "Fair, resolving this.",
        ]);
        const links = screen.getAllByRole("link", { name: "Open comment on Example Forge" });
        expect(links.map((link) => link.getAttribute("href"))).toEqual([
          threadUrl,
          threadUrl,
          threadUrl,
        ]);
        expect(keyWarnings()).toEqual([]);
      });

      it("keeps comments apart when they match on URL, time, and author", () => {
        const keyWarnings = watchKeyWarnings();
        state.info!.data = withPr({
          comments: [comment({ body: "First of two" }), comment({ body: "Second of two" })],
        });
        renderPanel();

        expect(commentBodies()).toEqual(["First of two", "Second of two"]);
        expect(keyWarnings()).toEqual([]);
      });

      it("keeps an unchanged comment card mounted when a poll adds a reply above it", () => {
        const keyWarnings = watchKeyWarnings();
        const first = comment({
          body: "Thread one",
          created_at: "2026-09-05T07:00:00.000Z",
          url: `${prUrl}?discussionId=1`,
        });
        const second = comment({
          author: "Grace Hopper",
          author_id: "u-grace",
          body: "Thread two",
          created_at: "2026-09-05T08:00:00.000Z",
          url: `${prUrl}?discussionId=2`,
        });
        const reply = comment({
          body: "Reply in thread one",
          created_at: "2026-09-05T09:00:00.000Z",
          url: first.url,
        });
        state.info!.data = withPr({ comments: [first, second] });
        const { rerender } = renderPanel();
        const card = screen.getByText("Thread two").closest("li");

        // The next poll returns a reply to the first thread, ahead of the second.
        state.info = { ...state.info!, data: withPr({ comments: [first, reply, second] }) };
        rerender(<PullRequestPanel conversationId="conv_1" />);

        expect(commentBodies()).toEqual(["Thread one", "Reply in thread one", "Thread two"]);
        expect(screen.getByText("Thread two").closest("li")).toBe(card);
        expect(keyWarnings()).toEqual([]);
      });

      it("lists every check in the hover card when checks share a build URL or a name", async () => {
        const user = userEvent.setup();
        const keyWarnings = watchKeyWarnings();
        const buildUrl = "https://forge.example.test/contoso/web/_build/results?buildId=41";
        state.info!.data = withPr({
          checks: {
            passing: 4,
            failing: 0,
            pending: 0,
            total: 4,
            runs: [
              { name: "Build", bucket: "passing", url: buildUrl },
              { name: "Lint", bucket: "passing", url: buildUrl },
              { name: "Deploy", bucket: "passing", url: null },
              { name: "Deploy", bucket: "passing", url: null },
            ],
          },
        });
        renderPanel();

        await user.hover(screen.getByRole("button", { name: /4\s*passed/ }));
        const rows = await screen.findAllByRole("listitem");
        expect(rows.map((row) => row.textContent)).toEqual(["Build", "Lint", "Deploy", "Deploy"]);
        expect(keyWarnings()).toEqual([]);
      });
    });
  });

  it("explains a diff the host can't produce for a PR outside the workspace", async () => {
    state.diff = {
      object: "session.github.pr_diff",
      patch: "",
      unavailable_reason: "pr_outside_workspace",
    };
    renderChanges();
    expect(
      await screen.findByText("Diff unavailable for a PR outside this workspace's repository"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it.each([
    [undefined, "Diff unavailable. Refresh or open the request on its provider."],
    ["The provider truncated this diff.", "The provider truncated this diff."],
  ])("explains provider diff failures (%s)", async (message, expected) => {
    state.diff = {
      object: "session.github.pr_diff",
      patch: "",
      unavailable_reason: "truncated",
      message,
    };
    renderChanges();

    expect(await screen.findByText(expected!)).toBeInTheDocument();
    expect(screen.queryByText(/outside this workspace/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("reports a failed diff fetch instead of silently rendering empty file sections", async () => {
    state.diff = null;
    state.diffError = new Error("Diff request timed out.");
    renderChanges();

    expect(await screen.findByText("Diff request timed out.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
  });

  it("marks an incomplete changed-file list while retaining loaded file diffs", async () => {
    state.changes!.data!.has_more = true;
    renderChanges();

    expect(await screen.findByText("The changed-file list is incomplete.")).toBeInTheDocument();
    expect(screen.getAllByTestId("diff")).toHaveLength(2);
  });

  it("reports an unavailable file list without claiming the request has no changes", async () => {
    state.changes!.data = {
      available: true,
      data: [],
      warning: "Changed-file lookup failed.",
    };
    renderChanges();

    expect(await screen.findByText("Changed-file lookup failed.")).toBeInTheDocument();
    expect(screen.getAllByText("Changed files are unavailable.").length).toBeGreaterThan(0);
    expect(screen.queryByText("No changes vs base.")).not.toBeInTheDocument();
  });

  it("shows a no-open-PR empty state (naming the branch) and hides the diff", () => {
    state.info!.data!.pr = null;
    renderPanel();
    expect(screen.getByText(/No open PR for/)).toBeInTheDocument();
    expect(screen.getByText("test/pr-view")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
    expect(screen.queryByRole("button", { name: "Link a PR" })).not.toBeInTheDocument();
  });

  it("offers linking beneath the empty-state description when tracking is available", () => {
    state.info!.data!.pr = null;
    state.info!.data!.prs = [];
    state.info!.data!.tracking_available = true;
    renderPanel();
    const description = screen.getByText(/Pull requests created in this session appear here/);
    const link = screen.getByRole("button", { name: "Link a PR" });
    expect(description.parentElement).toContainElement(link);
    expect(screen.queryByRole("combobox", { name: "Session pull request" })).toBeNull();
    fireEvent.click(link);
    expect(description.parentElement).toContainElement(
      screen.getByRole("textbox", { name: "Pull request URL" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Pull request URL" })).toBeNull();
    fireEvent.click(link);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Pull request URL" }), {
      key: "Escape",
    });
    expect(screen.queryByRole("textbox", { name: "Pull request URL" })).toBeNull();
  });

  it("shows the diff immediately when unifiedLineCount is at the threshold", async () => {
    state.parsedFiles = [{ name: "hello.py", unifiedLineCount: LARGE_DIFF_THRESHOLD }];
    state.changes = {
      data: { available: true, data: [file("hello.py", "modified")] },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderChanges();
    // Exactly at the threshold: diff renders, no affordance.
    expect(await screen.findByTestId("diff")).toBeInTheDocument();
    expect(screen.queryByText(/Large diff/)).toBeNull();
  });

  it("replaces a file's diff with a large-diff affordance when unifiedLineCount exceeds the threshold", async () => {
    state.parsedFiles = [
      { name: "hello.py", unifiedLineCount: LARGE_DIFF_THRESHOLD + 1 },
      { name: "src/app.ts" },
    ];
    renderChanges();
    // The oversized file shows the affordance; the normal file renders its diff.
    expect(await screen.findByText(/Large diff/)).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(`${(LARGE_DIFF_THRESHOLD + 1).toLocaleString()}\\s*lines`)),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show diff" })).toBeInTheDocument();
    const diffs = screen.getAllByTestId("diff");
    expect(diffs).toHaveLength(1);
    expect(diffs[0].getAttribute("data-path")).toBe("src/app.ts");
  });

  it("reveals the diff after clicking Show diff", async () => {
    state.parsedFiles = [{ name: "hello.py", unifiedLineCount: LARGE_DIFF_THRESHOLD + 1 }];
    state.changes = {
      data: { available: true, data: [file("hello.py", "modified")] },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderChanges();
    await screen.findByText(/Large diff/);
    fireEvent.click(screen.getByRole("button", { name: "Show diff" }));
    expect(await screen.findByTestId("diff")).toHaveAttribute("data-path", "hello.py");
    expect(screen.queryByText(/Large diff/)).toBeNull();
  });
});

describe("derivePullRequestPanelState", () => {
  const ready: PullRequestInfo = {
    object: "session.github.info",
    available: true,
    gh_available: true,
    authenticated: true,
    branch: "feat/x",
    base_ref: "main",
    repo: { name_with_owner: "acme/app" },
    pr: {
      number: 1,
      title: "t",
      state: "OPEN",
      url: "u",
      is_draft: false,
      author: "a",
      base_ref: "main",
      head_ref: "feat/x",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    },
  };
  const q = (over: Partial<{ isLoading: boolean; error: unknown; data: PullRequestInfo }>) => ({
    isLoading: false,
    error: null as unknown,
    data: undefined as PullRequestInfo | undefined,
    ...over,
  });

  it("orders transient states ahead of data", () => {
    expect(derivePullRequestPanelState(q({ isLoading: true })).kind).toBe("loading");
    expect(derivePullRequestPanelState(q({ error: new RunnerOfflineError() })).kind).toBe(
      "runner-offline",
    );
    expect(derivePullRequestPanelState(q({ error: new Error("boom") })).kind).toBe("error");
  });

  it("maps each unavailable reason to its own state", () => {
    expect(derivePullRequestPanelState(q({ data: undefined })).kind).toBe("unavailable");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "no_os_env" } }),
      ).kind,
    ).toBe("unavailable");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "not_a_git_repo" } }),
      ).kind,
    ).toBe("not-a-git-repo");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "host_outdated" } }),
      ).kind,
    ).toBe("host-outdated");
  });

  it("walks the gh layer: cli → auth → repo → pr → ready", () => {
    expect(derivePullRequestPanelState(q({ data: { ...ready, gh_available: false } }))).toEqual({
      kind: "no-cli",
      cli: "gh",
    });
    expect(derivePullRequestPanelState(q({ data: { ...ready, authenticated: false } })).kind).toBe(
      "repo-unresolved",
    );
    expect(derivePullRequestPanelState(q({ data: { ...ready, repo: null } })).kind).toBe(
      "repo-unresolved",
    );
    const noPr = derivePullRequestPanelState(q({ data: { ...ready, pr: null } }));
    expect(noPr).toEqual({ kind: "no-pr", branch: "feat/x" });
    expect(derivePullRequestPanelState(q({ data: ready }))).toEqual({ kind: "ready" });
  });

  it("reads the remote host and the provider's auth from the payload", () => {
    expect(
      derivePullRequestPanelState(
        q({
          data: {
            object: "session.github.info",
            available: false,
            reason: "unsupported_remote",
            remote_host: "git.example.com",
          },
        }),
      ),
    ).toEqual({ kind: "unsupported-remote", remoteHost: "git.example.com" });
    // `auth` wins over the legacy fields, which still say gh is ready.
    const auth: PullRequestAuth = {
      authenticated: true,
      hint: null,
      cli: { name: "forge", available: true },
      accounts: null,
      selected_account: null,
    };
    const az = (over: Partial<PullRequestAuth>) => ({ ...ready, auth: { ...auth, ...over } });
    expect(derivePullRequestPanelState(q({ data: az({ authenticated: false }) })).kind).toBe(
      "repo-unresolved",
    );
    expect(derivePullRequestPanelState(q({ data: az({}) }))).toEqual({ kind: "ready" });
  });

  it("stops at a missing CLI only while signed out", () => {
    const azNoCli = (authenticated: boolean, over: Partial<PullRequestInfo> = {}) => ({
      ...ready,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      auth: {
        authenticated,
        hint: null,
        cli: { name: "forge", available: false },
        accounts: null,
        selected_account: null,
      },
      ...over,
    });
    expect(derivePullRequestPanelState(q({ data: azNoCli(false) }))).toEqual({
      kind: "no-cli",
      cli: "forge",
    });
    // A token signs in without the CLI, so the signed-in flow goes on as usual.
    expect(derivePullRequestPanelState(q({ data: azNoCli(true) }))).toEqual({ kind: "ready" });
    expect(derivePullRequestPanelState(q({ data: azNoCli(true, { pr: null }) }))).toEqual({
      kind: "no-pr",
      branch: "feat/x",
    });
    expect(derivePullRequestPanelState(q({ data: azNoCli(true, { repo: null }) })).kind).toBe(
      "repo-unresolved",
    );
  });
});

describe("session PR selection", () => {
  it("keeps the selector and repository identity while PR details load or fail", async () => {
    const user = userEvent.setup();
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/42";
    state.info = {
      isLoading: false,
      error: null,
      isFetching: false,
      data: {
        ...state.info!.data!,
        object: "session.github.info",
        available: true,
        gh_available: true,
        authenticated: true,
        tracking_available: true,
        selected_pr_url: one,
        pr: { ...state.info!.data!.pr!, url: one, number: 42, title: "First repository" },
        prs: [
          {
            url: one,
            host: "github.com",
            repository: "example/one",
            number: 42,
            title: "First repository",
            relationship: "created",
          },
          {
            url: two,
            host: "github.com",
            repository: "example/two",
            number: 42,
            title: "Second repository",
            relationship: "created",
          },
        ],
      },
    };
    const { rerender } = renderPanel();
    const picker = screen.getByRole("combobox", { name: "Session pull request" });
    expect(picker).toHaveTextContent("example/one #42 — First repository");
    expect(picker).not.toHaveAttribute("title");
    await user.hover(picker);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "example/one #42 — First repository",
    );
    await user.unhover(picker);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    await user.click(picker);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "example/one #42 — First repository" }),
      ).toHaveAttribute("aria-selected", "true"),
    );
    const secondOption = screen.getByRole("option", {
      name: "example/two #42 — Second repository",
    });
    expect(secondOption).toBeVisible();
    expect(secondOption).not.toHaveAttribute("title");
    await user.hover(secondOption);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "example/two #42 — Second repository",
    );
    await user.click(secondOption);
    expect(picker).toHaveTextContent("example/two #42 — Second repository");
    expect(picker).not.toHaveAttribute("title");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", { poll: true, prUrl: two });
    await user.hover(picker);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "example/two #42 — Second repository",
    );
    await user.unhover(picker);

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByRole("combobox", { name: "Session pull request" })).toBe(picker);
    expect(picker).toHaveTextContent("example/two #42 — Second repository");
    expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
    expect(screen.queryByText("First repository")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Unlink PR" })).toBeEnabled();
    expect(screen.queryByRole("link", { name: "Open the PR on GitHub" })).toBeNull();

    state.info = { isLoading: false, error: new Error("Metadata unavailable"), isFetching: false };
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByRole("combobox", { name: "Session pull request" })).toBe(picker);
    expect(picker).toHaveTextContent("example/two #42 — Second repository");
    const errorMessage = screen.getByText(/Metadata unavailable/);
    const fallback = screen.getByRole("link", { name: "Open the PR on GitHub" });
    expect(errorMessage.parentElement).toContainElement(fallback);
    expect(fallback).toHaveAttribute("href", two);
    await user.click(picker);
    await user.click(screen.getByRole("option", { name: "example/one #42 — First repository" }));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", { poll: true, prUrl: one });

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<PullRequestPanel conversationId="conv_other" />);
    expect(screen.queryByRole("combobox", { name: "Session pull request" })).toBeNull();
  });

  it.each([undefined, null, "", "   "])(
    "falls back to the PR identity when its title is %j",
    (title) => {
      const url = "https://github.com/example/one/pull/42";
      Object.assign(state.info!.data!, {
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          {
            url,
            host: "github.com",
            repository: "example/one",
            number: 42,
            title,
            relationship: "created",
          },
        ],
      });
      renderPanel();
      const picker = screen.getByRole("combobox", { name: "Session pull request" });
      expect(picker).toHaveTextContent(/^example\/one #42$/);
      expect(picker).not.toHaveAttribute("title");
      fireEvent.click(picker);
      expect(screen.getByRole("option", { name: "example/one #42" })).toBeVisible();
    },
  );

  it("keeps the host and inferred marker alongside a trimmed PR title", () => {
    const url = "https://github.example.com/example/one/pull/42";
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [
        {
          url,
          host: "github.example.com",
          repository: "example/one",
          number: 42,
          title: "  Fix session selection  ",
          relationship: "inferred",
        },
      ],
    });
    renderPanel();
    const picker = screen.getByRole("combobox", { name: "Session pull request" });
    const label = "github.example.com/example/one #42 (from branch) — Fix session selection";
    expect(picker).toHaveTextContent(label);
    expect(picker).not.toHaveAttribute("title");
    fireEvent.click(picker);
    expect(screen.getByRole("option", { name: label })).toBeVisible();
  });

  it("passes the selected PR and revisions into file queries", () => {
    const url = "https://github.com/example/two/pull/42";
    state.info = {
      isLoading: false,
      error: null,
      isFetching: false,
      data: {
        object: "session.github.info",
        available: true,
        gh_available: true,
        authenticated: true,
        selected_pr_url: url,
        pr: {
          number: 42,
          title: "Second repo",
          url,
          state: "OPEN",
          is_draft: false,
          author: "user",
          head_ref: "topic",
          base_ref: "main",
          head_sha: "head",
          base_sha: "base",
          checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
        },
      },
    };
    renderPanel();
    expect(usePullRequestChangedFiles).toHaveBeenLastCalledWith("conv_1", true, url, "base:head");
  });
});
