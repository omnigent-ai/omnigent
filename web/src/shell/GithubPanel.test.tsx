// Tests for GithubPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GithubChangedFile, GithubInfo } from "@/hooks/useGithub";

const state = vi.hoisted(() => ({
  info: null as {
    data?: GithubInfo;
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  changes: null as {
    data?: { available: boolean; data: GithubChangedFile[] };
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  // Per-file diffs the stubbed parsePatchFiles yields (name + optional
  // rename fields), so tests can exercise renamed/pure-rename rendering.
  parsedFiles: [] as { name: string; prevName?: string; type?: string }[],
}));

vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: vi.fn(() => state.info),
  useGithubChangedFiles: vi.fn(() => state.changes),
  // One whole-PR patch; the panel parses it into per-file diffs.
  useGithubPrDiff: () => ({
    data: { object: "session.github.pr_diff", patch: "PATCH" },
    isLoading: false,
    error: null,
    isFetching: false,
  }),
  fetchGithubFileContents: async () => ({ before: "old", after: "new" }),
  // The account selector (shown in the repo-unresolved empty state) calls this;
  // stub the mutation shape it reads.
  useUpdateSessionPr: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useSetGithubPreference: () => ({
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

import { useGithubInfo, useGithubChangedFiles } from "@/hooks/useGithub";

import { GithubPanel, deriveGithubPanelState } from "./GithubPanel";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";

function file(
  path: string,
  status: GithubChangedFile["status"],
  adds = 1,
  dels = 0,
): GithubChangedFile {
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
  return render(<GithubPanel conversationId="conv_1" />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

function renderHostPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <GithubPanel target={{ kind: "host", hostId: "host_1", workspace: "/work/repo" }} />,
    {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    },
  );
}

/** Render, then switch to the Changes tab — Summary is the default, so the
 *  diff view (and its toolbar) only exists after activating Changes. Radix
 *  tabs select on pointer-down, so mouseDown (not click) flips the tab. */
function renderChanges() {
  const r = renderPanel();
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
  return r;
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
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("GithubPanel", () => {
  it("keeps host workspace GitHub data read-only and target-scoped", () => {
    state.info!.data!.tracking_available = true;
    state.info!.data!.selected_pr_url = "https://example.com/pr/6000";
    state.info!.data!.prs = [
      {
        url: "https://example.com/pr/6000",
        host: "github.com",
        repository: "acme/app",
        number: 6000,
        relationship: "attached",
      },
    ];
    renderHostPanel();

    const target = { kind: "host", hostId: "host_1", workspace: "/work/repo" };
    expect(useGithubInfo).toHaveBeenCalledWith(target, { poll: true, prUrl: undefined });
    expect(useGithubChangedFiles).toHaveBeenCalledWith(
      target,
      true,
      "https://example.com/pr/6000",
      ":",
    );
    expect(screen.getByText("chore: dummy PR")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link a PR" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unlink PR" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Session pull request" }),
    ).not.toBeInTheDocument();
  });

  it("describes a host branch without implying a session exists", () => {
    state.info!.data!.pr = null;
    state.info!.data!.prs = [];
    renderHostPanel();

    expect(
      screen.getByText(
        "No open pull request was found for this branch in the upstream repository.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/created in this session/i)).not.toBeInTheDocument();
  });

  it("shows the PR title in the header and CI check pills on the Summary tab", async () => {
    renderPanel();
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
    ["MERGED", "Merged", "text-brand-accent"],
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

  it("shows Summary empty states when the PR has no body or comments", () => {
    // The default fixture carries neither a body nor comments.
    renderPanel();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
    expect(screen.getByText("No comments yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
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
    state.info = {
      data: { object: "session.github.info", available: false, reason: "host_outdated" },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("Update your host to use GitHub")).toBeInTheDocument();
    expect(screen.getByText(/0\.13\.0 or later/)).toBeInTheDocument();
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
});

describe("deriveGithubPanelState", () => {
  const ready: GithubInfo = {
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
  const q = (over: Partial<{ isLoading: boolean; error: unknown; data: GithubInfo }>) => ({
    isLoading: false,
    error: null as unknown,
    data: undefined as GithubInfo | undefined,
    ...over,
  });

  it("orders transient states ahead of data", () => {
    expect(deriveGithubPanelState(q({ isLoading: true })).kind).toBe("loading");
    expect(deriveGithubPanelState(q({ error: new RunnerOfflineError() })).kind).toBe(
      "runner-offline",
    );
    expect(deriveGithubPanelState(q({ error: new Error("boom") })).kind).toBe("error");
  });

  it("maps each unavailable reason to its own state", () => {
    expect(deriveGithubPanelState(q({ data: undefined })).kind).toBe("unavailable");
    expect(
      deriveGithubPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "no_os_env" } }),
      ).kind,
    ).toBe("unavailable");
    expect(
      deriveGithubPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "not_a_git_repo" } }),
      ).kind,
    ).toBe("not-a-git-repo");
    expect(
      deriveGithubPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "host_outdated" } }),
      ).kind,
    ).toBe("host-outdated");
  });

  it("walks the gh layer: cli → auth → repo → pr → ready", () => {
    expect(deriveGithubPanelState(q({ data: { ...ready, gh_available: false } })).kind).toBe(
      "no-gh-cli",
    );
    expect(deriveGithubPanelState(q({ data: { ...ready, authenticated: false } })).kind).toBe(
      "repo-unresolved",
    );
    expect(deriveGithubPanelState(q({ data: { ...ready, repo: null } })).kind).toBe(
      "repo-unresolved",
    );
    const noPr = deriveGithubPanelState(q({ data: { ...ready, pr: null } }));
    expect(noPr).toEqual({ kind: "no-pr", branch: "feat/x" });
    expect(deriveGithubPanelState(q({ data: ready }))).toEqual({ kind: "ready" });
  });
});

describe("session PR selection", () => {
  it("keeps the selector and repository identity while PR details load or fail", async () => {
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
            relationship: "created",
          },
          {
            url: two,
            host: "github.com",
            repository: "example/two",
            number: 42,
            relationship: "created",
          },
        ],
      },
    };
    const { rerender } = renderPanel();
    const picker = screen.getByRole("combobox", { name: "Session pull request" });
    expect(picker).toHaveTextContent("example/one #42");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    fireEvent.click(picker);
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "example/one #42" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    fireEvent.click(screen.getByRole("option", { name: "example/two #42" }));
    expect(picker).toHaveTextContent("example/two #42");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(useGithubInfo).toHaveBeenLastCalledWith("conv_1", { poll: true, prUrl: two });

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<GithubPanel conversationId="conv_1" />);
    expect(screen.getByRole("combobox", { name: "Session pull request" })).toBe(picker);
    expect(picker).toHaveTextContent("example/two #42");
    expect(screen.getByText("Loading GitHub…")).toBeInTheDocument();
    expect(screen.queryByText("First repository")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Unlink PR" })).toBeEnabled();
    expect(screen.queryByRole("link", { name: "Open the PR on GitHub" })).toBeNull();

    state.info = { isLoading: false, error: new Error("Metadata unavailable"), isFetching: false };
    rerender(<GithubPanel conversationId="conv_1" />);
    expect(screen.getByRole("combobox", { name: "Session pull request" })).toBe(picker);
    const errorMessage = screen.getByText(/Metadata unavailable/);
    const fallback = screen.getByRole("link", { name: "Open the PR on GitHub" });
    expect(errorMessage.parentElement).toContainElement(fallback);
    expect(fallback).toHaveAttribute("href", two);
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("option", { name: "example/one #42" }));
    expect(useGithubInfo).toHaveBeenLastCalledWith("conv_1", { poll: true, prUrl: one });

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<GithubPanel conversationId="conv_other" />);
    expect(screen.queryByRole("combobox", { name: "Session pull request" })).toBeNull();
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
    expect(useGithubChangedFiles).toHaveBeenLastCalledWith("conv_1", true, url, "base:head");
  });
});
