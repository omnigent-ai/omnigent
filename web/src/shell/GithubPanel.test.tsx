// Tests for GithubPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import { render, screen, fireEvent } from "@testing-library/react";
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
  useGithubInfo: () => state.info,
  useGithubChangedFiles: () => state.changes,
  // One whole-PR patch; the panel parses it into per-file diffs.
  useGithubPrDiff: () => ({
    data: { object: "session.github.pr_diff", patch: "PATCH" },
    isLoading: false,
    error: null,
    isFetching: false,
  }),
  fetchGithubFileContents: async () => ({ before: "old", after: "new" }),
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
  return render(
    <QueryClientProvider client={client}>
      <GithubPanel conversationId="conv_1" />
    </QueryClientProvider>,
  );
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
  it("shows the PR header with title and CI check pills", async () => {
    renderPanel();
    expect(await screen.findByText("chore: dummy PR")).toBeInTheDocument();
    expect(screen.getByText("#6000")).toBeInTheDocument();
    // CI checks are on their own line as labeled pills (not a diffstat). A
    // zero bucket (pending) renders no pill.
    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByText(/66\s*passed/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*failed/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/)).toBeNull();
  });

  it("stacks a diff section per changed file", async () => {
    renderPanel();
    const diffs = await screen.findAllByTestId("diff");
    expect(diffs.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py", "src/app.ts"]);
  });

  it("jumps to a file's section when its sidebar row is clicked", async () => {
    renderPanel();
    await screen.findAllByTestId("diff");
    // Both the sidebar row and the section header are buttons matching the
    // name; the sidebar row (which scrolls) is first in the DOM.
    const row = screen.getAllByRole("button", { name: /app\.ts/ })[0];
    fireEvent.click(row);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("collapses a file's diff when its section header is clicked", async () => {
    renderPanel();
    expect(await screen.findAllByTestId("diff")).toHaveLength(2);
    // The section header carries aria-expanded; the sidebar row doesn't.
    const header = screen.getByRole("button", { name: /app\.ts/, expanded: true });
    fireEvent.click(header);
    // Only the other file's diff remains rendered.
    const remaining = screen.getAllByTestId("diff");
    expect(remaining.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py"]);
  });

  it("collapses and expands every diff from the toolbar", async () => {
    renderPanel();
    expect(await screen.findAllByTestId("diff")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Collapse all diffs" }));
    expect(screen.queryAllByTestId("diff")).toHaveLength(0);
    // The same button now offers the inverse action.
    fireEvent.click(screen.getByRole("button", { name: "Expand all diffs" }));
    expect(screen.getAllByTestId("diff")).toHaveLength(2);
  });

  it("hides and shows the file sidebar from the toolbar", async () => {
    renderPanel();
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
    renderPanel();
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
    renderPanel();
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
    renderPanel();
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
    renderPanel();
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

  it("shows a no-open-PR empty state (naming the branch) and hides the diff", () => {
    state.info!.data!.pr = null;
    renderPanel();
    expect(screen.getByText(/No open PR for/)).toBeInTheDocument();
    expect(screen.getByText("test/pr-view")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();
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
