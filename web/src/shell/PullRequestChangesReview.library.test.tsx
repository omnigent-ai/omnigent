// The Changes tab's review on the real CodeView: where conversations and drafts land.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { PULL_REQUEST_REVIEW_TREE_WIDTH } from "@/components/diff-review/reviewTreeWidth";
import { resetRememberedPaneWidthForTesting } from "@/hooks/useRememberedPaneWidth";
import { readPanelSharePreference } from "@/lib/panelSizePreferences";
import { installResizableWidth } from "@/test/resizableWidth";
import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PullRequestChangesReview } from "./PullRequestChangesReview";
import { NO_TEXT_DIFF, type PullRequestPatchState } from "./pullRequestReviewSource";
import type { ReviewThreadContext } from "../newPullRequestPage/shell/PullRequestReviewThread";
import { newReviewThreadDraftKey } from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner } from "@/lib/workspaceDraftOwner";

const PATH = "src/app.py";
const PATCH = [
  `diff --git a/${PATH} b/${PATH}`,
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -1,3 +1,3 @@",
  " first",
  "-old",
  "+new",
  " last",
  "",
].join("\n");

function thread(id: string, overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    path: PATH,
    side: "RIGHT",
    line: 2,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "@@ -1,3 +1,3 @@\n first\n-old\n+new",
    comments: {
      data: [
        { id: `${id}-root`, author: "reviewer", body: `About ${id}`, created_at: null, url: null },
      ],
      has_more: false,
      next_cursor: null,
    },
    ...overrides,
  };
}

const context: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: "https://example.com/acme/app/pull/1",
  account: "octocat",
  canReply: true,
  canCreate: true,
  headSha: "head",
  baseSha: "base",
  loadComments: () => undefined,
  loadingComments: null,
};

const changedFile = {
  path: PATH,
  name: "app.py",
  bytes: 1,
  modified_at: null,
  status: "modified",
  lines_added: 1,
  lines_removed: 1,
} as PullRequestChangedFile;

interface ReviewSetup {
  files?: PullRequestChangedFile[];
  filesByPath?: Map<string, FileDiffMetadata>;
  threadsByFile?: Map<string, ReviewThread[]>;
  ownedPaths?: ReadonlySet<string> | null;
  patch?: PullRequestPatchState;
  onRetryPatch?: () => void;
}

function renderReview(
  threads: ReviewThread[],
  client = new QueryClient(),
  setup: ReviewSetup = {},
) {
  return render(reviewElement(threads, client, setup));
}

function reviewElement(threads: ReviewThread[], client: QueryClient, setup: ReviewSetup) {
  const fileDiff = parsePatchFiles(PATCH)[0]!.files[0]!;
  return (
    <QueryClientProvider client={client}>
      <div style={{ height: 600 }}>
        <PullRequestChangesReview
          files={setup.files ?? [changedFile]}
          filesByPath={setup.filesByPath ?? new Map([[PATH, fileDiff]])}
          threadsByFile={setup.threadsByFile ?? new Map([[PATH, threads]])}
          elsewhereThreads={[thread("elsewhere", { path: "gone.py" })]}
          context={context}
          matchingRevision
          revision="base:head"
          layout="unified"
          onLayoutChange={() => undefined}
          focusedThread={null}
          unresolvedCount={threads.length}
          onNavigateUnresolved={() => undefined}
          loadingThreads={false}
          ownedPaths={setup.ownedPaths}
          patch={setup.patch}
          onRetryPatch={setup.onRetryPatch}
        />
      </div>
    </QueryClientProvider>
  );
}

const parsed = (patch: string[]) => parsePatchFiles(patch.join("\n"))[0]!.files[0]!;
const oneLinePatch = (path: string) =>
  parsed([
    `diff --git a/${path} b/${path}`,
    `--- a/${path}`,
    `+++ b/${path}`,
    "@@ -1 +1 @@",
    "-a",
    "+b",
    "",
  ]);
/** The files' titles, top to bottom, from each diff's shadow root. */
const shownOrder = () =>
  [...document.querySelectorAll("diffs-container")].map(
    (item) => item.shadowRoot?.querySelector("[data-title]")?.textContent,
  );
const fileThread = (id: string, path: string) =>
  thread(id, { path, subject_type: "FILE", line: null, diff_hunk: "" });

/**
 * The slot a conversation is rendered through: its light-DOM slot, which only
 * shows when the file's shadow root renders a matching slot.
 */
function renderedSlotOf(id: string): string | null {
  const element = document.querySelector(`[data-review-thread="${id}"]`);
  const slotted = element?.closest("[slot]");
  const name = slotted?.getAttribute("slot");
  const host = slotted?.closest("diffs-container");
  if (!name || !host?.shadowRoot?.querySelector(`slot[name="${name}"]`)) return null;
  return name;
}

/** A conversation after the files, outside every file's diff. */
function inFooter(element: Element | null): boolean {
  return !!element && !element.closest("diffs-container") && !element.closest("[slot]");
}

describe("PullRequestChangesReview with the real CodeView", () => {
  beforeAll(() => {
    Element.prototype.scrollTo ??= function scrollTo() {};
    Element.prototype.scrollBy ??= function scrollBy() {};
  });

  it("shows conversations under their lines, on their side, inside the diff", async () => {
    renderReview([thread("new-side"), thread("old-side", { side: "LEFT", line: 2 })]);
    await waitFor(() => expect(renderedSlotOf("new-side")).toBe("annotation-additions-2"));
    expect(renderedSlotOf("old-side")).toBe("annotation-deletions-2");
    const conversation = document.querySelector('[data-review-thread="new-side"]')!;
    expect(conversation.closest("diffs-container")).not.toBeNull();
  });

  it("shows conversations no line shows above the file, and others after the files", async () => {
    renderReview([thread("outdated", { outdated: true, original_line: 2 }), thread("placed")]);
    await waitFor(() => expect(renderedSlotOf("placed")).toBe("annotation-additions-2"));
    expect(renderedSlotOf("outdated")).toBe("annotation-additions-0");
    const elsewhere = document.querySelector('[data-review-thread="elsewhere"]')!;
    expect(elsewhere.closest("diffs-container")).toBeNull();
    expect(elsewhere.closest("[slot]")).toBeNull();
  });

  it("opens a saved draft's composer on the line it was started from", async () => {
    const client = new QueryClient();
    client.setQueryData(
      newReviewThreadDraftKey(draftOwner(), context.conversationId, context.prUrl, PATH),
      {
        body: "Keep this?",
        pending: false,
        error: null,
        anchor: {
          path: PATH,
          side: "LEFT",
          line: 2,
          start_line: null,
          head_sha: "head",
          base_sha: "base",
        },
      },
    );
    renderReview([], client);
    await waitFor(() =>
      expect(document.querySelector('[aria-label="New review conversation"]')).not.toBeNull(),
    );
    const composer = document.querySelector('[aria-label="New review conversation"]')!;
    const slot = composer.closest("[slot]")?.getAttribute("slot");
    expect(slot).toBe("annotation-deletions-2");
    const host = composer.closest("diffs-container");
    await waitFor(() =>
      expect(host?.shadowRoot?.querySelector(`slot[name="${slot}"]`)).not.toBeNull(),
    );
    expect(composer.textContent).toContain("New conversation · Old line 2");
  });

  it("shows a conversation above a deleted file on the side it renders", async () => {
    const deleted = parsed([
      `diff --git a/${PATH} b/${PATH}`,
      "deleted file mode 100644",
      `--- a/${PATH}`,
      "+++ /dev/null",
      "@@ -1,2 +0,0 @@",
      "-first",
      "-second",
      "",
    ]);
    renderReview([], new QueryClient(), {
      files: [{ ...changedFile, status: "deleted" }],
      filesByPath: new Map([[PATH, deleted]]),
      threadsByFile: new Map([[PATH, [fileThread("on-deleted", PATH)]]]),
    });
    await waitFor(() => expect(renderedSlotOf("on-deleted")).toBe("annotation-deletions-0"));
  });

  it("lists a binary file's conversations after the files", async () => {
    const binary = parsed([
      "diff --git a/logo.png b/logo.png",
      "index 1111111..2222222 100644",
      "Binary files a/logo.png and b/logo.png differ",
      "",
    ]);
    renderReview([], new QueryClient(), {
      files: [{ ...changedFile, path: "logo.png", name: "logo.png" }],
      filesByPath: new Map([["logo.png", binary]]),
      threadsByFile: new Map([["logo.png", [fileThread("on-logo", "logo.png")]]]),
    });
    await waitFor(() =>
      expect(inFooter(document.querySelector('[data-review-thread="on-logo"]'))).toBe(true),
    );
  });

  it("reviews a textual SVG patch as text", async () => {
    const svg = parsed([
      "diff --git a/icon.svg b/icon.svg",
      "--- a/icon.svg",
      "+++ b/icon.svg",
      "@@ -1 +1 @@",
      '-<svg width="16"/>',
      '+<svg width="24"/>',
      "",
    ]);
    renderReview([], new QueryClient(), {
      files: [{ ...changedFile, path: "icon.svg", name: "icon.svg" }],
      filesByPath: new Map([["icon.svg", svg]]),
      threadsByFile: new Map(),
    });
    await waitFor(() =>
      expect(
        document.querySelector("diffs-container")?.shadowRoot?.querySelector("[data-line]"),
      ).not.toBeNull(),
    );
    expect(document.body.textContent).not.toContain("Binary file");
  });

  it("keeps a saved draft on a file whose patch has no diff after the files", async () => {
    const client = new QueryClient();
    client.setQueryData(
      newReviewThreadDraftKey(draftOwner(), context.conversationId, context.prUrl, PATH),
      {
        body: "Keep this?",
        pending: false,
        error: null,
        anchor: {
          path: PATH,
          side: "RIGHT",
          line: 2,
          start_line: null,
          head_sha: "older",
          base_sha: "base",
        },
      },
    );
    renderReview([], client, { filesByPath: new Map(), threadsByFile: new Map() });
    await waitFor(() =>
      expect(document.querySelector('[aria-label="New review conversation"]')).not.toBeNull(),
    );
    const composer = document.querySelector('[aria-label="New review conversation"]')!;
    expect(inFooter(composer)).toBe(true);
    expect(composer.querySelector("textarea")?.value).toBe("Keep this?");
    expect(composer.textContent).toContain("select its lines again before posting");
  });

  describe("before the patch arrives", () => {
    const other = { ...changedFile, path: "src/other.py", name: "other.py" };
    const header = (path: string) =>
      screen.queryByRole("button", { name: `More actions for ${path}` });

    it("lists every file with its diff loading, then fills them in when the patch lands", async () => {
      const client = new QueryClient();
      const loading: ReviewSetup = {
        files: [changedFile, other],
        filesByPath: new Map(),
        patch: { status: "loading" },
      };
      const view = render(reviewElement([thread("placed")], client, loading));
      await waitFor(() => expect(header(PATH)).not.toBeNull());
      expect(header("src/other.py")).not.toBeNull();
      expect(screen.getAllByText("Loading diff…")).toHaveLength(2);
      // Its conversation waits for the diff rather than being listed as without one.
      expect(document.querySelector('[data-review-thread="placed"]')).toBeNull();

      const ready = parsePatchFiles(PATCH)[0]!.files[0]!;
      view.rerender(
        reviewElement([thread("placed")], client, {
          ...loading,
          filesByPath: new Map([[PATH, ready]]),
          patch: { status: "ready" },
        }),
      );
      await waitFor(() => expect(renderedSlotOf("placed")).toBe("annotation-additions-2"));
      expect(screen.queryByText("Loading diff…")).toBeNull();
      // A listed file the patch has no section for says so.
      expect(screen.getByText(NO_TEXT_DIFF)).toBeInTheDocument();
    });

    it("says on each file that the patch failed, and every Try again reads it once more", async () => {
      const onRetryPatch = vi.fn();
      renderReview([thread("placed")], new QueryClient(), {
        files: [changedFile, other],
        filesByPath: new Map(),
        patch: { status: "failed", message: "GitHub didn't answer in time." },
        onRetryPatch,
      });
      const retries = await screen.findAllByRole("button", { name: "Try again" });
      expect(retries).toHaveLength(2);
      expect(screen.queryByText("Loading diff…")).toBeNull();
      fireEvent.click(retries[1]!);
      expect(onRetryPatch).toHaveBeenCalledTimes(1);
      // Its conversation stays in reach, after the files.
      await waitFor(() =>
        expect(inFooter(document.querySelector('[data-review-thread="placed"]'))).toBe(true),
      );
    });
  });

  it("lists the files in the tree's order, folders first, not in the order GitHub sent", async () => {
    // GitHub sorts byte-wise: capitals first, root files among folders.
    const paths = ["README.md", "src/Zeta.py", "src/alpha.py"];
    renderReview([], new QueryClient(), {
      files: paths.map((path) => ({ ...changedFile, path, name: path })),
      filesByPath: new Map(paths.map((path) => [path, oneLinePatch(path)])),
      threadsByFile: new Map(),
    });
    await waitFor(() => expect(shownOrder()).toEqual(["src/alpha.py", "src/Zeta.py", "README.md"]));
  });

  it("keeps files where they are when ownership arrives later, marking them at once", async () => {
    const paths = ["a.py", "b.py", "c.py"];
    const listed: ReviewSetup = {
      files: paths.map((path) => ({ ...changedFile, path, name: path })),
      filesByPath: new Map(paths.map((path) => [path, oneLinePatch(path)])),
      threadsByFile: new Map(),
    };
    const client = new QueryClient();
    const view = render(reviewElement([], client, listed));
    await waitFor(() => expect(shownOrder()).toEqual(["a.py", "b.py", "c.py"]));
    view.rerender(reviewElement([], client, { ...listed, ownedPaths: new Set(["c.py"]) }));
    await waitFor(() => expect(screen.getAllByText("Owned by you")).toHaveLength(1));
    expect(shownOrder()).toEqual(["a.py", "b.py", "c.py"]);
  });

  it("marks the files the reader owns in place, and can show only those", async () => {
    const paths = ["a.py", "b.py", "c.py"];
    renderReview([], new QueryClient(), {
      files: paths.map((path) => ({ ...changedFile, path, name: path })),
      filesByPath: new Map(paths.map((path) => [path, oneLinePatch(path)])),
      threadsByFile: new Map(),
      ownedPaths: new Set(["c.py"]),
    });
    await waitFor(() => expect(shownOrder()).toEqual(["a.py", "b.py", "c.py"]));
    expect(screen.getAllByText("Owned by you")).toHaveLength(1);

    fireEvent.pointerDown(screen.getByRole("button", { name: "Filter changed files" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: /Show only files you own/ }),
    );
    await waitFor(() => expect(shownOrder()).toEqual(["c.py"]));
  });
});

describe("the Changes tree beside a pull request's diff", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    resetRememberedPaneWidthForTesting(PULL_REQUEST_REVIEW_TREE_WIDTH);
  });

  it("keeps its share of the review, so widening the panel widens the tree too", async () => {
    resetRememberedPaneWidthForTesting(PULL_REQUEST_REVIEW_TREE_WIDTH);
    const resize = installResizableWidth(900);
    renderReview([]);
    const edge = await screen.findByRole("separator", { name: "Resize changed files" });
    const column = () => document.querySelector<HTMLElement>("[data-diff-review-tree-column]")!;
    // 240px fits the narrowest review that docks the tree, 720px; 900px gets 300px.
    expect(column().style.width).toBe("300px");
    expect(edge).toHaveAttribute("aria-valuemin", "160");
    expect(edge).toHaveAttribute("aria-valuemax", "432");

    // The tree docks at the right, so its edge is its left one: ArrowLeft widens it.
    fireEvent.keyDown(edge, { key: "ArrowLeft" });
    expect(column().style.width).toBe("312px");
    expect(readPanelSharePreference("pullRequestReviewTreeShare")).toBeCloseTo(312 / 900, 4);
    act(() => resize(1200));
    expect(column().style.width).toBe("416px");
    // It grows with its share up to its 520px limit.
    fireEvent.keyDown(edge, { key: "End" });
    expect(column().style.width).toBe("520px");
  });
});
