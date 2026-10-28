// Tests for PullRequestPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { parsePatchFiles } from "@pierre/diffs";
import type * as DiffsModule from "@pierre/diffs";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type * as UseSessionPullRequestsModule from "@/hooks/useSessionPullRequests";
import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestAuth,
  PullRequestChangedFile,
  PullRequestComment,
  PullRequestDiffResponse,
  PullRequestInfo,
  PullRequestRepoLabels,
  PullRequestReviewCandidates,
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
  canEdit: true,
  repoLabels: {
    data: undefined as PullRequestRepoLabels | undefined,
    isPending: true,
    isError: false,
    error: null as Error | null,
  },
  updateLabels: { mutate: vi.fn(), isError: false, error: null as Error | null },
  reviewCandidates: {
    data: undefined as PullRequestReviewCandidates | undefined,
    isPending: true,
    isError: false,
    error: null as Error | null,
  },
  updateReviewers: { mutate: vi.fn(), isError: false, error: null as Error | null },
  // The server's stored PRs per session; absent sessions keep what the panel seeded.
  stored: {} as Record<string, PullRequestAssociation[]>,
  // Sessions the server hasn't imported yet; others are synced.
  unsynced: new Set<string>(),
  storedFetches: 0,
  storesPrs: true,
  unlink: { mutate: vi.fn(), isPending: false, isError: false, error: null as Error | null },
  sandboxAsleep: false,
  // False once the session's host disconnects, which leaves cached live info without an error.
  serveable: undefined as boolean | undefined,
}));

vi.mock("@/hooks/usePullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UsePullRequestsModule>();
  return {
    // The panel reads every info payload through the real normalizer.
    normalizePullRequestInfo: original.normalizePullRequestInfo,
    usePullRequestCommentComposer: original.usePullRequestCommentComposer,
    pullRequestCommentTooLong: original.pullRequestCommentTooLong,
    MAX_PULL_REQUEST_COMMENT_CHARACTERS: original.MAX_PULL_REQUEST_COMMENT_CHARACTERS,
    applyPullRequestLabelChange: original.applyPullRequestLabelChange,
    pullRequestLabelsMutationKey: original.pullRequestLabelsMutationKey,
    usePullRequestRepoLabels: vi.fn(() => state.repoLabels),
    useUpdatePullRequestLabels: () => state.updateLabels,
    usePullRequestMergeOptions: () => ({ data: undefined, isPending: true, isError: false }),
    useMergePullRequest: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useDisablePullRequestAutoMerge: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    applyPullRequestReviewerChange: original.applyPullRequestReviewerChange,
    pullRequestReviewerKey: original.pullRequestReviewerKey,
    pullRequestReviewersMutationKey: original.pullRequestReviewersMutationKey,
    usePullRequestReviewCandidates: vi.fn(() => state.reviewCandidates),
    useUpdatePullRequestReviewers: () => state.updateReviewers,
    useUpdatePullRequestDetails: () => ({
      mutate: vi.fn(),
      reset: vi.fn(),
      isPending: false,
      isError: false,
    }),
    uploadPullRequestAttachment: vi.fn(),
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
  };
});
vi.mock("@/hooks/useSessionPullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UseSessionPullRequestsModule>();
  const { useQuery } = await import("@tanstack/react-query");
  return {
    sessionPullRequestsKey: original.sessionPullRequestsKey,
    storedListToShow: original.storedListToShow,
    storesSessionPullRequests: () => state.storesPrs,
    useSessionPullRequests: (id: string | undefined) => {
      return useQuery({
        queryKey: original.sessionPullRequestsKey(id),
        queryFn: async () => {
          state.storedFetches += 1;
          return { prs: state.stored[id ?? ""] ?? [], synced: !state.unsynced.has(id ?? "") };
        },
        // Sessions without a server list here read live info only.
        enabled: !!id && id in state.stored && state.storesPrs,
      });
    },
    useUnlinkSessionPr: () => state.unlink,
  };
});
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => state.canEdit }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof UseWorkspaceChangedFilesModule>()),
  useWorkspaceAsleep: () => state.sandboxAsleep,
  useWorkspaceServeable: () => state.serveable,
}));

// The diff rendering (@pierre/diffs) is exercised by the library itself; here
// we only assert a section renders one diff per parsed file. parsePatchFiles is
// stubbed to yield the files configured on `state` (name + optional rename
// metadata), matching the whole-PR patch.
vi.mock("@pierre/diffs", () => ({
  parsePatchFiles: vi.fn(() => [{ files: state.parsedFiles }]),
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

import {
  usePullRequestInfo,
  usePullRequestChangedFiles,
  usePullRequestRepoLabels,
  usePullRequestReviewCandidates,
} from "@/hooks/usePullRequests";

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
  adds: number | null = 1,
  dels: number | null = 0,
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

/** Render, then switch to the Comments tab (see renderChanges). */
function renderComments() {
  const r = renderPanel();
  fireEvent.mouseDown(screen.getByRole("tab", { name: /^Comments/ }));
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

/** The markdown body of each comment card, in rendered order. Only the active
 *  tab mounts, so on the Comments tab every markdown block is a comment. */
function commentBodies(): (string | null)[] {
  return screen.getAllByTestId("markdown").map((el) => el.textContent);
}

let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // The diff-layout toggle seeds from persisted prefs; start each test clean.
  window.localStorage.clear();
  state.stored = {};
  state.unsynced = new Set();
  state.storedFetches = 0;
  state.storesPrs = true;
  state.unlink = { mutate: vi.fn(), isPending: false, isError: false, error: null };
  state.sandboxAsleep = false;
  state.serveable = undefined;
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
  state.canEdit = true;
  state.repoLabels = { data: undefined, isPending: true, isError: false, error: null };
  state.updateLabels = { mutate: vi.fn(), isError: false, error: null };
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
    // Title + number live in the shared header (both tabs).
    expect(await screen.findByText("chore: dummy PR")).toBeInTheDocument();
    expect(screen.getByText("#6000")).toBeInTheDocument();
    // CI checks render on the Summary tab (the default) as labeled pills. A
    // zero bucket (pending) renders no pill.
    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByText(/66\s*passed/)).toBeInTheDocument();
    expect(screen.getByText(/2\s*failed/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/)).toBeNull();
    expect(screen.queryByRole("button", { name: /cancelled|skipped/ })).toBeNull();
  });

  it("shows cancelled and skipped checks as separate grey pills listing their jobs", async () => {
    const user = userEvent.setup();
    state.info!.data!.pr!.checks = {
      passing: 0,
      failing: 0,
      pending: 0,
      cancelled: 1,
      skipped: 2,
      total: 3,
      runs: [
        { name: "cancelled job", bucket: "cancelled", url: null },
        { name: "skipped job", bucket: "skipped", url: null },
        { name: "another skipped job", bucket: "skipped", url: null },
      ],
    };
    renderPanel();

    for (const label of ["1 cancelled", "2 skipped"]) {
      expect(screen.getByRole("button", { name: label })).toHaveClass(
        "bg-muted",
        "text-muted-foreground",
        "rounded-full",
      );
    }
    expect(screen.queryByRole("button", { name: /passed|failed|pending/ })).toBeNull();

    await user.hover(screen.getByRole("button", { name: "2 skipped" }));
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual(["skipped job", "another skipped job"]);
  });

  it("shows each assigned reviewer with its current state and team kind", () => {
    state.info!.data!.pr!.reviewers = [
      { login: "alice", kind: "user", state: "PENDING" },
      { login: "bob", kind: "user", state: "APPROVED" },
      { login: "carol", kind: "user", state: "COMMENTED" },
      { login: "platform-team", kind: "team", state: "CHANGES_REQUESTED" },
      { login: "former-reviewer", kind: "user", state: "DISMISSED" },
    ];
    renderPanel();

    expect(screen.getByRole("heading", { name: "Reviewers" })).toBeInTheDocument();
    for (const [subject, status] of [
      ["alice", "Pending"],
      ["bob", "Approved"],
      ["carol", "Commented"],
      ["Team platform-team", "Changes requested"],
      ["former-reviewer", "Dismissed"],
    ]) {
      expect(screen.getByRole("listitem", { name: `${subject}: ${status}` })).toBeInTheDocument();
    }
    expect(screen.getByText("(team)")).toBeInTheDocument();

    const reviewersSection = screen.getByRole("heading", { name: "Reviewers" }).closest("section");
    const checksSection = screen.getByRole("heading", { name: "Checks" }).closest("section");
    expect(reviewersSection).not.toBeNull();
    expect(checksSection).not.toBeNull();
    expect(
      reviewersSection!.compareDocumentPosition(checksSection!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows an empty reviewer section when the host reports no reviewers", () => {
    state.info!.data!.pr!.reviewers = [];
    renderPanel();

    expect(screen.getByRole("heading", { name: "Reviewers" })).toBeInTheDocument();
    expect(screen.getByText("No reviewers assigned.")).toBeInTheDocument();
  });

  it("omits reviewers for a host that does not include the optional field", () => {
    renderPanel();

    expect(screen.queryByRole("heading", { name: "Reviewers" })).toBeNull();
  });

  it("updates reviewers when the active PR changes", () => {
    state.info!.data!.pr!.reviewers = [{ login: "alice", kind: "user", state: "PENDING" }];
    const { rerender } = renderPanel();
    expect(screen.getByRole("listitem", { name: "alice: Pending" })).toBeInTheDocument();

    state.info!.data!.pr = {
      ...state.info!.data!.pr!,
      number: 6001,
      title: "chore: another PR",
      reviewers: [{ login: "platform-team", kind: "team", state: "APPROVED" }],
    };
    rerender(<PullRequestPanel conversationId="conv_1" />);

    expect(screen.queryByRole("listitem", { name: "alice: Pending" })).toBeNull();
    expect(
      screen.getByRole("listitem", { name: "Team platform-team: Approved" }),
    ).toBeInTheDocument();
  });

  describe("title and description editing", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        details_editing: true,
        attachment_uploads: true,
      };
    });

    it("opens the title and description editors on the selected PR", () => {
      renderPanel();

      fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
      expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue(
        state.info!.data!.pr!.title,
      );
      fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
      expect(screen.getByRole("textbox", { name: "Pull request description" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Attach images or videos" })).toBeInTheDocument();
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without details editing",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
      [
        "a host that names no selected PR",
        () => {
          delete state.info!.data!.selected_pr_url;
        },
      ],
    ])("offers no title or description editing to %s", (_caller, setup) => {
      setup();
      renderPanel();

      expect(screen.queryByRole("button", { name: "Edit title" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit description" })).not.toBeInTheDocument();
    });
  });

  describe("reviewer editing", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.pr!.reviewers = [
        { login: "alice", kind: "user", state: "PENDING" },
        { login: "bob", kind: "user", state: "APPROVED" },
      ];
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        reviewer_editing: true,
      };
    });

    it("wires the rows and the picker to the selected PR", () => {
      renderPanel();

      fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from alice" }));
      expect(state.updateReviewers.mutate).toHaveBeenLastCalledWith({
        prUrl,
        add: [],
        remove: [{ login: "alice", kind: "user", state: "PENDING" }],
      });
      fireEvent.click(screen.getByRole("button", { name: "Re-request review from bob" }));
      expect(state.updateReviewers.mutate).toHaveBeenLastCalledWith({
        prUrl,
        add: [{ login: "bob", kind: "user", state: "APPROVED" }],
        remove: [],
      });
      expect(usePullRequestReviewCandidates).toHaveBeenLastCalledWith("conv_1", prUrl, false);
      fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
      expect(usePullRequestReviewCandidates).toHaveBeenLastCalledWith("conv_1", prUrl, true);
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without reviewer editing",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
      [
        "a host that names no selected PR",
        () => {
          delete state.info!.data!.selected_pr_url;
        },
      ],
    ])("shows reviewers read-only for %s", (_caller, setup) => {
      setup();
      renderPanel();

      expect(screen.getByRole("listitem", { name: "alice: Pending" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Request reviewers" })).toBeNull();
      expect(screen.queryByRole("button", { name: /review request from|review from/ })).toBeNull();
    });
  });

  describe("labels", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";
    const bug = { name: "bug", color: "d73a4a", description: "Something isn't working" };
    const docs = { name: "Docs", color: "0075ca", description: null };

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.pr!.labels = [docs, bug];
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        label_editing: true,
      };
    });

    it("lists the PR's labels by name between reviewers and checks", () => {
      state.info!.data!.pr!.reviewers = [];
      renderPanel();

      const list = screen.getByRole("list", { name: "Labels" });
      expect(
        within(list)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual(["bug", "Docs"]);
      expect(within(list).getAllByRole("listitem")[0]).toHaveAttribute(
        "title",
        "Something isn't working",
      );
      const labels = list.closest("section")!;
      const reviewers = screen.getByRole("heading", { name: "Reviewers" }).closest("section")!;
      const checks = screen.getByText("Checks").closest("section")!;
      expect(
        reviewers.compareDocumentPosition(labels) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(
        labels.compareDocumentPosition(checks) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      // Reviewers and labels share one row, split by a vertical divider, above a divider.
      const row = reviewers.parentElement!.parentElement!;
      expect(row).toContainElement(labels);
      expect(row.querySelector('[data-slot="separator"]')).toHaveAttribute(
        "data-orientation",
        "vertical",
      );
      expect(row.nextElementSibling).toHaveAttribute("data-slot", "separator");
    });

    it("wires editing to the selected PR: chips remove, and the picker loads on open", () => {
      renderPanel();

      fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
      expect(state.updateLabels.mutate).toHaveBeenLastCalledWith({
        prUrl,
        add: [],
        remove: [bug],
      });
      expect(usePullRequestRepoLabels).toHaveBeenLastCalledWith("conv_1", prUrl, false);
      fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
      expect(usePullRequestRepoLabels).toHaveBeenLastCalledWith("conv_1", prUrl, true);
      expect(screen.getByRole("combobox", { name: "Filter labels" })).toBeInTheDocument();
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without label editing",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
    ])("shows labels read-only for %s", (_caller, setup) => {
      setup();
      renderPanel();

      expect(screen.getByRole("list", { name: "Labels" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit labels" })).toBeNull();
      expect(screen.queryByRole("button", { name: /^Remove label/ })).toBeNull();
    });

    it.each([
      [
        "does not report labels",
        () => {
          delete state.info!.data!.pr!.labels;
        },
      ],
      [
        "names no selected PR",
        () => {
          delete state.info!.data!.selected_pr_url;
        },
      ],
    ])("omits labels when the host %s", (_case, setup) => {
      setup();
      renderPanel();

      expect(screen.queryByRole("heading", { name: "Labels" })).toBeNull();
    });
  });

  describe("merge button", () => {
    beforeEach(() => {
      state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
      state.info!.data!.pr!.head_sha = "a".repeat(40);
      state.info!.data!.pr!.merge_state_status = "CLEAN";
      state.info!.data!.pr!.auto_merge = null;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        merging: true,
      };
    });

    it("sits below the checks and opens the merge dialog", () => {
      renderPanel();

      const merge = screen.getByRole("region", { name: "Merge" });
      const checks = screen.getByText("Checks").closest("section")!;
      expect(checks.compareDocumentPosition(merge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      fireEvent.click(within(merge).getByRole("button", { name: "Merge pull request" }));
      expect(screen.getByText("Merge pull request #6000?")).toBeInTheDocument();
    });

    it("divides the metadata from the description", () => {
      renderPanel();

      const divider = [...document.querySelectorAll('[data-slot="separator"]')].at(-1)!;
      const merge = screen.getByRole("region", { name: "Merge" });
      const description = screen.getByText("Description").closest("section")!;
      expect(
        merge.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(
        divider.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without merging",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
      [
        "a provider that can't merge",
        () => {
          state.info!.data!.capabilities!.merging = false;
        },
      ],
      [
        "a host that doesn't report the merge state",
        () => {
          delete state.info!.data!.pr!.merge_state_status;
        },
      ],
      [
        "a closed PR",
        () => {
          state.info!.data!.pr!.state = "CLOSED";
        },
      ],
      [
        "a merged PR",
        () => {
          state.info!.data!.pr!.state = "MERGED";
        },
      ],
    ])("is hidden for %s", (_case, setup) => {
      setup();
      renderPanel();

      expect(screen.queryByRole("region", { name: "Merge" })).toBeNull();
    });
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

  it("lands on Summary with the description and counts comments on their own tab", async () => {
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
    // Comments have their own tab, which shows how many there are.
    expect(screen.queryByText("Looks good to me!")).toBeNull();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Summary",
      "Comments 1",
      "Changes",
    ]);

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Comments 1" }));
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(screen.getByText("Looks good to me!")).toBeInTheDocument();
    expect(screen.queryByText(/This PR does the thing\./)).toBeNull();
    expect(screen.queryByText("Checks")).toBeNull();
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
    renderComments();

    expect(screen.getByRole("tab", { name: "Comments 4" })).toBeInTheDocument();
    expect(commentBodies()).toEqual(["First", "Second", "Third", "Fourth"]);
    expect(keyWarnings()).toEqual([]);
  });

  it.each([
    ["shared suffixes", ["zoe_data", "amy_data", "a-bot", "z-bot"]],
    ["other suffixes", ["build[bot]", "lint[bot]", "zoe_acme", "amy_acme"]],
    ["ordinary usernames", ["ci[bot]", "bob", "alice"]],
  ])("spaces comment colors consistently for %s", (_, orderedAuthors) => {
    const comments = [...orderedAuthors].reverse().map((author) => ({
      author,
      body: `Comment by ${author}`,
      created_at: null,
      url: null,
    }));
    const repeatedAuthor = orderedAuthors[0]!.toUpperCase();
    state.info!.data!.pr!.comments = [
      ...comments,
      { author: repeatedAuthor, body: "Another comment", created_at: null, url: null },
      { author: null, body: "Deleted author", created_at: null, url: null },
    ];
    const assertColors = () => {
      orderedAuthors.forEach((author, index) => {
        expect(screen.getByText(author).previousElementSibling).toHaveStyle({
          backgroundColor: `oklch(0.48 0.13 ${(index * 360) / orderedAuthors.length})`,
          color: "#fff",
        });
      });
      expect(screen.getByText(repeatedAuthor).previousElementSibling).toHaveStyle({
        backgroundColor: "oklch(0.48 0.13 0)",
      });
      expect(screen.getByText("unknown").previousElementSibling).not.toHaveAttribute("style");
    };
    const view = renderComments();
    assertColors();
    view.unmount();
    state.info!.data!.pr!.comments.reverse();
    renderComments();
    assertColors();
  });

  it("shows empty states when the PR has no body or comments", () => {
    // The default fixture carries neither a body nor comments.
    renderPanel();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();

    const commentsTab = screen.getByRole("tab", { name: "Comments" });
    expect(commentsTab).toHaveTextContent(/^Comments\s*$/);
    fireEvent.mouseDown(commentsTab);
    expect(screen.getByText("No comments yet.")).toBeInTheDocument();
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
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Comments" }));
    expect(screen.getByText("Some comments are unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("No comments yet.")).not.toBeInTheDocument();
  });

  it("explains unreadable checks instead of showing empty counts", () => {
    // The token read the PR but GitHub refused its check runs.
    state.info!.data!.pr!.checks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };
    state.info!.data!.pr!.checks_supported = false;
    renderPanel();

    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checks can’t be read with this GitHub token. Fine-grained personal access tokens can’t read check runs; use a classic token or a GitHub App.",
    );
    expect(screen.queryByText(/Some checks are unavailable/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /passed|failed|pending|cancelled|skipped/ }),
    ).toBeNull();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
  });

  it("retains loaded comments while indicating that more could not be fetched", () => {
    state.info!.data!.pr!.comments_partial = true;
    state.info!.data!.pr!.comments = [
      { author: "reviewer", body: "Loaded comment", created_at: null, url: null },
    ];
    renderComments();

    expect(screen.getByRole("tab", { name: "Comments 1+" })).toBeInTheDocument();
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

  it("keeps the no-PR state and linking available when another provider fails", () => {
    state.info!.data!.pr = null;
    state.info!.data!.tracking_available = true;
    state.info!.data!.discovery_warnings = ["GitLab discovery failed. Refresh to retry."];
    renderPanel();

    expect(screen.getByText(/No open PR for/)).toBeInTheDocument();
    // The empty state shows the panel title; the provider stays in the tooltip.
    expect(screen.getByRole("heading", { name: "Pull Requests" })).toHaveAttribute(
      "title",
      "GitHub",
    );
    expect(screen.getByText("Pull Requests")).not.toHaveClass("sr-only");
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("GitLab discovery failed.");
    expect(screen.queryByText("Pull requests aren’t available.")).not.toBeInTheDocument();
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
    // index.css binds @pierre/diffs' font variables on this class; outside it the
    // shadow stylesheet falls back to a fixed 13px that ignores the Interface font size.
    for (const diff of diffs) expect(diff.closest(".github-diff-stack")).not.toBeNull();
  });

  it("jumps to and expands a file's section when its sidebar row is clicked", async () => {
    renderChanges();
    await screen.findAllByTestId("diff");
    fireEvent.click(screen.getByRole("button", { name: /app\.ts/, expanded: true }));
    expect(screen.getAllByTestId("diff").map((d) => d.getAttribute("data-path"))).toEqual([
      "hello.py",
    ]);
    // Both the sidebar row and the section header are buttons matching the
    // name; the sidebar row (which scrolls) is first in the DOM.
    const row = screen.getAllByRole("button", { name: /app\.ts/ })[0];
    fireEvent.click(row);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
    expect(screen.getByRole("button", { name: /app\.ts/, expanded: true })).toBeInTheDocument();
    expect(screen.getAllByTestId("diff")).toHaveLength(2);
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

  it("names an extensible provider's account selector", () => {
    const info = state.info!.data!;
    info.repo = null;
    info.provider = "forge";
    info.provider_display = {
      id: "forge",
      display_name: "Example Forge",
      request_name: "pull request",
      number_prefix: "#",
    };
    info.auth = {
      authenticated: true,
      cli: { name: "forge", available: true },
      hint: null,
      accounts: ["personal", "work"].map((login) => ({
        login,
        active: login === "personal",
        state: "success",
        host: "forge.example.test",
      })),
      selected_account: "personal",
    };
    info.capabilities = {
      account_switching: true,
      base_remote_selection: false,
      line_counts: true,
      linked_pr_diff: true,
    };
    renderPanel();
    expect(screen.getByRole("combobox", { name: "Example Forge account" })).toBeInTheDocument();
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
      expect(screen.getByRole("heading", { name: "Pull Requests" })).toHaveAttribute(
        "title",
        "gitlab",
      );
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
        renderComments();

        expect(screen.getByRole("tab", { name: "Comments 3" })).toBeInTheDocument();
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
        renderComments();

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
        const { rerender } = renderComments();
        const card = screen.getByText("Thread two").closest("li");

        // The next poll returns a reply to the first thread, ahead of the second.
        state.info = { ...state.info!, data: withPr({ comments: [first, reply, second] }) };
        rerender(<PullRequestPanel conversationId="conv_1" />);

        expect(commentBodies()).toEqual(["Thread one", "Reply in thread one", "Thread two"]);
        expect(screen.getByText("Thread two").closest("li")).toBe(card);
        expect(keyWarnings()).toEqual([]);
      });

      it("lists every check in the hover card and links only http(s) run URLs, even when checks share a URL or a name", async () => {
        const user = userEvent.setup();
        const keyWarnings = watchKeyWarnings();
        const buildUrl = "https://forge.example.test/contoso/web/_build/results?buildId=41";
        state.info!.data = withPr({
          checks: {
            passing: 5,
            failing: 0,
            pending: 0,
            total: 5,
            runs: [
              { name: "Build", bucket: "passing", url: buildUrl },
              { name: "Lint", bucket: "passing", url: buildUrl },
              { name: "Deploy", bucket: "passing", url: null },
              { name: "Deploy", bucket: "passing", url: null },
              // A status poster controls its target URL, so a script URL must not render.
              { name: "Status", bucket: "passing", url: "javascript:alert(1)" },
            ],
          },
        });
        renderPanel();

        await user.hover(screen.getByRole("button", { name: /5\s*passed/ }));
        const rows = await screen.findAllByRole("listitem");
        expect(rows.map((row) => row.textContent)).toEqual([
          "Build",
          "Lint",
          "Deploy",
          "Deploy",
          "Status",
        ]);
        expect(
          rows.map((row) => within(row).queryByRole("link")?.getAttribute("href") ?? null),
        ).toEqual([buildUrl, buildUrl, null, null, null]);
        expect(within(rows[0]).getByRole("link")).toHaveAttribute("target", "_blank");
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

  it("reports a failed file list without claiming there are no changes", async () => {
    state.changes!.data = undefined;
    state.changes!.error = new Error("Could not load changed files.");
    renderChanges();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load changed files.");
    expect(screen.queryByText("No changes vs base.")).toBeNull();
  });

  it("reports a malformed patch", async () => {
    vi.mocked(parsePatchFiles).mockImplementationOnce(() => {
      throw new Error("bad patch");
    });
    renderChanges();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t read the PR diff.");
    expect(screen.queryByText("No text diff available for this file.")).toBeNull();
  });

  it("refreshes info, files, diff and the stored PRs even from an error state", async () => {
    state.info!.error = new Error("Request timed out.");
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries").mockResolvedValue();
    onTestFinished(() => invalidate.mockRestore());
    renderPanel();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey?.[0])).toEqual([
      "github-info",
      "github-changed-files",
      "github-pr-diff",
      "session-pull-requests",
    ]);
  });

  it("marks an incomplete changed-file list while retaining loaded file diffs", async () => {
    state.changes!.data!.has_more = true;
    renderChanges();

    expect(await screen.findByText("The changed-file list is incomplete.")).toBeInTheDocument();
    expect(screen.getAllByTestId("diff")).toHaveLength(2);
  });

  it.each([false, true])(
    "keeps missing text patches local to their files (all missing: %s)",
    async (allMissing) => {
      const parser = await vi.importActual<typeof DiffsModule>("@pierre/diffs");
      if (!allMissing) vi.mocked(parsePatchFiles).mockImplementationOnce(parser.parsePatchFiles);
      state.changes!.data!.data = [
        file("__init__.py", "created", null, null),
        ...(!allMissing ? [file("widget.py", "modified", 1, 1)] : []),
        file("image.png", "modified", null, null),
      ];
      state.diff = {
        object: "session.github.pr_diff",
        patch: allMissing
          ? ""
          : "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n@@ -1 +1 @@\n-old\n+new\n",
      };
      const { container } = renderChanges();

      expect(await screen.findAllByText("No text diff available for this file.")).toHaveLength(2);
      for (const path of ["__init__.py", "image.png"]) {
        const section = container.querySelector<HTMLElement>(`[data-github-file="${path}"]`)!;
        expect(within(section).getByText("No text diff available for this file.")).toBeVisible();
        expect(within(section).queryByTestId("diff")).toBeNull();
        expect(within(section).queryByText("+0")).toBeNull();
      }
      expect(screen.queryAllByTestId("diff").map((diff) => diff.dataset.path)).toEqual(
        allMissing ? [] : ["widget.py"],
      );
      expect(screen.queryByText("No changes vs base.")).toBeNull();
      expect(screen.queryByText("The changed-file list is incomplete.")).toBeNull();
    },
  );

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
    const input = screen.getByRole("textbox", { name: "Pull request URL" });
    expect(description.parentElement).toContainElement(input);
    expect(input).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Link a PR" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Pull request URL" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Link a PR" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Pull request URL" }), {
      key: "Escape",
    });
    expect(screen.queryByRole("textbox", { name: "Pull request URL" })).toBeNull();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeInTheDocument();
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

  it("reads an offline sandbox as sandbox-offline unless info was cached before it stopped", () => {
    const asleep = (over: Parameters<typeof q>[0]) => ({ ...q(over), sandboxAsleep: true });
    expect(derivePullRequestPanelState(asleep({ isLoading: true })).kind).toBe("sandbox-offline");
    expect(derivePullRequestPanelState(asleep({})).kind).toBe("sandbox-offline");
    expect(derivePullRequestPanelState(asleep({ data: ready })).kind).toBe("ready");
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
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveTextContent(/^$/);
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

  it("shows the server's stored PRs and lets them be unlinked while the host is offline", async () => {
    const user = userEvent.setup();
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/7";
    const checkedAt = Date.UTC(2026, 9, 22, 17, 30) / 1000;
    state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
    state.stored.conv_1 = [
      {
        url: one,
        host: "github.com",
        repository: "example/one",
        number: 42,
        title: "Ship it",
        relationship: "created",
        state: "MERGED",
        is_draft: false,
        state_checked_at: checkedAt,
      },
      {
        url: two,
        host: "github.com",
        repository: "example/two",
        number: 7,
        title: null,
        relationship: "inferred",
      },
    ];
    renderPanel();

    const picker = await screen.findByRole("combobox", { name: "Session pull request" });
    expect(picker).toHaveTextContent("example/one #42 — Ship it");
    const stored = screen.getByTestId("github-stored-pr");
    expect(within(stored).getByRole("link", { name: /Ship it/ })).toHaveAttribute("href", one);
    expect(within(stored).getByLabelText("Pull request status: Merged")).toBeInTheDocument();
    expect(within(stored).getByRole("status")).toHaveTextContent(
      `Last sync time: ${new Date(checkedAt * 1000).toLocaleString()}. To load the PR details, please reconnect the session’s host.`,
    );
    // Linking verifies the PR with the host's account; unlinking is the server's.
    expect(
      screen.getByRole("button", { name: "Link a PR (needs the host online)" }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Unlink PR" }));
    expect(state.unlink.mutate).toHaveBeenCalledWith(one, expect.anything());
    const [, { onSuccess }] = state.unlink.mutate.mock.calls[0];
    act(() => onSuccess({ prs: state.stored.conv_1.slice(1), synced: true, complete: true }));
    expect(picker).toHaveTextContent("example/two #7 (from branch)");
    // A PR the host never fetched has no sync time to show.
    expect(within(screen.getByTestId("github-stored-pr")).getByRole("status")).toHaveTextContent(
      /^To load the PR details, please reconnect the session’s host\.$/,
    );
  });

  it("switches cached live info to the stored PR once the host goes offline", async () => {
    const url = "https://github.com/acme/app/pull/6000";
    const attached: PullRequestAssociation = {
      url,
      host: "github.com",
      repository: "acme/app",
      number: 6000,
      title: "Attached while online",
      relationship: "attached",
      state: "OPEN",
      is_draft: false,
      state_checked_at: Date.now() / 1000 - 60,
    };
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [attached],
    });
    state.stored.conv_1 = [attached];
    const { rerender } = renderPanel();
    expect(
      await screen.findByRole("combobox", { name: "Session pull request" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("github-stored-pr")).toBeNull();

    // React Query keeps the last payload when the refetch fails.
    state.info = { ...state.info!, error: new RunnerOfflineError() };
    rerender(<PullRequestPanel conversationId="conv_1" />);

    const stored = screen.getByTestId("github-stored-pr");
    expect(within(stored).getByRole("link", { name: /Attached while online/ })).toHaveAttribute(
      "href",
      url,
    );
    expect(within(stored).getByLabelText("Pull request status: Open")).toBeInTheDocument();
    expect(screen.queryByText(/The agent is asleep/)).toBeNull();
  });

  it("treats an empty server list as authoritative while the host is offline", async () => {
    const url = "https://github.com/acme/app/pull/6000";
    const cached: PullRequestAssociation = {
      url,
      host: "github.com",
      repository: "acme/app",
      number: 6000,
      title: "Unlinked in another tab",
      relationship: "attached",
    };
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [cached],
    });
    state.info = { ...state.info!, error: new RunnerOfflineError() };
    state.stored.conv_1 = [];
    renderPanel();

    await waitFor(() =>
      expect(screen.queryByRole("combobox", { name: "Session pull request" })).toBeNull(),
    );
    expect(screen.queryByTestId("github-stored-pr")).toBeNull();
  });

  it.each([
    {
      offline: "the host is disconnected",
      serveable: false,
      sandboxAsleep: false,
      message: "To link a PR, please reconnect the session’s host.",
    },
    {
      offline: "the sandbox is asleep",
      serveable: undefined,
      sandboxAsleep: true,
      message: "To link a PR, send a message to start the session’s sandbox.",
    },
  ])(
    "asks for the host instead of offering to link a PR while $offline",
    async ({ serveable, sandboxAsleep, message }) => {
      state.serveable = serveable;
      state.sandboxAsleep = sandboxAsleep;
      // Cached from before the host went away, as after unlinking the last PR offline.
      Object.assign(state.info!.data!, {
        tracking_available: true,
        prs: [],
        pr: null,
        selected_pr_url: undefined,
      });
      state.stored.conv_1 = [];
      renderPanel();

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.getByText(/No open PR for/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Link a PR/ })).toBeNull();
    },
  );

  it("still offers to link a PR on an older server while the host is disconnected", () => {
    state.storesPrs = false;
    state.serveable = false;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      prs: [],
      pr: null,
      selected_pr_url: undefined,
    });
    renderPanel();

    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
  });

  it("says when the server hasn't imported the session's PRs yet", async () => {
    state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
    state.stored.conv_1 = [];
    state.unsynced.add("conv_1");
    renderPanel();

    expect(
      await screen.findByText(
        "This session’s PRs haven’t synced to the server yet. To load them, please reconnect the session’s host.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps an older server's offline panel as it was", () => {
    state.storesPrs = false;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: "https://github.com/acme/app/pull/6000",
      prs: [
        {
          url: "https://github.com/acme/app/pull/6000",
          host: "github.com",
          repository: "acme/app",
          number: 6000,
          title: "Live only",
          relationship: "attached",
        },
      ],
    });
    state.info = { ...state.info!, error: new RunnerOfflineError() };
    renderPanel();

    expect(screen.queryByTestId("github-stored-pr")).toBeNull();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
  });

  it.each(["javascript:alert(1)", "http://github.com/acme/app/pull/1"])(
    "links a stored PR only over https and marks a draft: %s",
    async (url) => {
      state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
      state.stored.conv_1 = [
        {
          url,
          host: "github.com",
          repository: "acme/app",
          number: 1,
          title: "Not a link",
          relationship: "created",
          state: "OPEN",
          is_draft: true,
        },
      ];
      renderPanel();

      // The details remount once the selection settles on the stored PR.
      await waitFor(() =>
        expect(
          within(screen.getByTestId("github-stored-pr")).getByLabelText(
            "Pull request status: Draft",
          ),
        ).toBeInTheDocument(),
      );
      const stored = screen.getByTestId("github-stored-pr");
      expect(within(stored).queryByRole("link")).toBeNull();
      expect(within(stored).getByText("Not a link")).toBeInTheDocument();
    },
  );

  it("asks to start a sleeping sandbox rather than reconnect a host", () => {
    const url = "https://github.com/acme/app/pull/6000";
    state.sandboxAsleep = true;
    state.info = { isLoading: false, error: null, isFetching: false };
    state.stored.conv_1 = [
      {
        url,
        host: "github.com",
        repository: "acme/app",
        number: 6000,
        title: "Sandbox PR",
        relationship: "created",
        state: "OPEN",
        is_draft: false,
        state_checked_at: null,
      },
    ];
    renderPanel();

    return waitFor(() =>
      expect(within(screen.getByTestId("github-stored-pr")).getByRole("status")).toHaveTextContent(
        /^To load the PR details, send a message to start the session’s sandbox\.$/,
      ),
    );
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

it.each([
  [true, true, true],
  [false, true, false],
  [true, false, false],
  [true, undefined, false],
])(
  "gates comment posting on edit access %s and host capability %s",
  (canEdit, capability, visible) => {
    state.canEdit = canEdit;
    state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
    state.info!.data!.capabilities = {
      account_switching: false,
      base_remote_selection: false,
      line_counts: false,
      linked_pr_diff: false,
      comment_posting: capability,
    };
    renderComments();
    expect(screen.queryByRole("textbox", { name: "New PR comment" }) !== null).toBe(visible);
  },
);

it.each(["capabilities", "selected_pr_url"] as const)(
  "hides comment posting without %s",
  (missing) => {
    state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
    state.info!.data!.capabilities = {
      account_switching: false,
      base_remote_selection: false,
      line_counts: false,
      linked_pr_diff: false,
      comment_posting: true,
    };
    if (missing === "capabilities") delete state.info!.data!.capabilities;
    else delete state.info!.data!.selected_pr_url;
    renderComments();
    expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();
    expect(screen.getByText("No comments yet.").parentElement).toHaveClass("pb-16");
  },
);
