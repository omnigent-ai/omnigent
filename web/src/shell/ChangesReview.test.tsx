import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useSyncExternalStore } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type * as WorkingCopyChangesModule from "@/hooks/useWorkingCopyChanges";
import type { WorkingCopyChanges } from "@/hooks/useWorkingCopyChanges";
import type { inMemoryCustomComparisons } from "@/lib/commitComparisons";
import type { RevisionSpec } from "@/lib/customComparison";
import type * as WorkingCopyComparisonsModule from "@/lib/workingCopyComparisons";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as BranchCommitsModule from "@/lib/branchCommits";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { openFindForActiveSurface } from "@/lib/findRouting";
import { resetShownReviewsForTests, shownReview } from "@/lib/reviewFileLinks";
import { localComparisonView, type LocalComparisonInput } from "@/lib/localComparison";
import { invalidateWorkingCopyChanges } from "@/lib/workingCopyComparisons";
import type { EffectiveComparison } from "@/lib/workingCopyComparison";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { ChangesReview, type ChangesReviewProps } from "./ChangesReview";
import { isFilePositionPending } from "./filePositionState";
import type { WorkingCopyDiffReviewBodyProps } from "./WorkingCopyDiffReview";

const mocks = vi.hoisted(() => ({
  source: vi.fn(),
  fetchBranchCommits: vi.fn(),
  body: vi.fn(),
  asleep: vi.fn(() => false),
  resume: vi.fn(),
  /** The open commit's comparisons, as its owner was given them. */
  commitComparisons: null as ReturnType<typeof inMemoryCustomComparisons> | null,
  commitOwner: { retry: vi.fn() },
  commitChanges: vi.fn<(spec: RevisionSpec | null) => WorkingCopyChanges>(),
  width: vi.fn(() => 0),
}));
// The open commit's owner: what it compares, read as its views read it.
vi.mock("@/lib/workingCopyComparisons", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkingCopyComparisonsModule>()),
  createComparisonOwner: ({
    customComparisons,
  }: {
    customComparisons: ReturnType<typeof inMemoryCustomComparisons>;
  }) => {
    mocks.commitComparisons = customComparisons;
    return { owner: mocks.commitOwner, dispose: vi.fn() };
  },
}));
vi.mock("@/hooks/useWorkingCopyChanges", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkingCopyChangesModule>()),
  useWorkingCopyChanges: () => {
    const comparisons = mocks.commitComparisons!;
    const spec = useSyncExternalStore(comparisons.subscribe, () => comparisons.read("s1", null!));
    return mocks.commitChanges(spec);
  },
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceAsleep: mocks.asleep,
}));
// The shared notice follows the same asleep signal the review reads.
vi.mock("@/hooks/useSandboxResume", () => ({
  useSandboxResume: () => ({
    status: mocks.asleep() ? "asleep" : "none",
    failure: null,
    availability: { available: true },
    resume: mocks.resume,
  }),
}));
vi.mock("./useLocalComparison", () => ({ useLocalComparison: mocks.source }));
vi.mock("@/hooks/useElementWidth", () => ({ useElementWidth: mocks.width }));
vi.mock("@/lib/branchCommits", async (importOriginal) => ({
  ...(await importOriginal<typeof BranchCommitsModule>()),
  fetchBranchCommits: mocks.fetchBranchCommits,
}));
vi.mock("./WorkingCopyDiffReview", () => ({
  WorkingCopyDiffReviewBody: (props: WorkingCopyDiffReviewBodyProps) => {
    mocks.body(props);
    return <div data-testid="review-body" />;
  },
}));
vi.mock("./fileCommands/useFileCommandFacts", () => ({
  useFileCommandEnvironment: () => ({}),
}));
vi.mock("./fileCommands/target", () => ({
  useSessionHostId: () => null,
  resolveFileCommandTarget: (path: string) => ({ path }),
}));
vi.mock("./fileCommands/reviewFileActions", () => ({ reviewFileActions: () => [] }));

const HEAD: EffectiveComparison = { base: "head", note: null, override: false, retry: false };

function changes(overrides: Partial<WorkingCopyChanges> = {}): WorkingCopyChanges {
  return {
    data: {
      available: true,
      data: [
        {
          path: "src/app.ts",
          name: "app.ts",
          status: "modified",
          bytes: 10,
          modified_at: 1,
          lines_added: 1,
          lines_removed: 1,
        },
      ],
    },
    isError: false,
    error: null,
    isSuccess: true,
    isLoading: false,
    dataUpdatedAt: 1_700_000_000_000,
    comparison: HEAD,
    support: { kind: "available" },
    revisionSupport: { kind: "unsupported", reason: "update_app", message: "Update" },
    custom: null,
    workspace: null,
    refetch: vi.fn(() => Promise.resolve()),
    generation: 1,
    permitted: true,
    accessEpoch: 0,
    readable: true,
    ...overrides,
  };
}

function source(shared: WorkingCopyChanges, overrides: Partial<LocalComparisonInput> = {}) {
  const actions = {
    refetchHead: vi.fn(),
    retryBranchPoint: vi.fn(),
    showLastCommit: vi.fn(),
    compareAgain: vi.fn(),
    compareRevisions: vi.fn(),
  };
  const view = localComparisonView({
    comparison: shared.comparison,
    headIsKnown: true,
    revisionSupport: shared.revisionSupport,
    custom: shared.custom,
    intent: "head",
    commands: [
      {
        id: "compare-automatic",
        label: "Automatic",
        name: "Automatic",
        checked: false,
        unavailableReason: null,
        run: vi.fn(),
      },
      {
        id: "compare-head",
        label: "Uncommitted Changes",
        name: "Uncommitted changes",
        checked: true,
        unavailableReason: null,
        run: vi.fn(),
      },
      {
        id: "refresh-changes",
        label: "Refresh Changes",
        name: "Refresh changes",
        unavailableReason: null,
        run: vi.fn(),
      },
    ],
    permitted: shared.permitted,
    readable: shared.readable,
    actions,
    ...overrides,
  });
  return { changes: shared, view, actions };
}

let client = new QueryClient();
function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
}

function renderReview(overrides: Partial<ChangesReviewProps> = {}) {
  const props: ChangesReviewProps = {
    conversationId: "s1",
    active: true,
    sort: "alpha",
    onViewFile: vi.fn(),
    onShowAllChanges: vi.fn(),
    onShowDefaultComparison: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<ChangesReview {...props} />, { wrapper }) };
}

afterEach(() => {
  resetShownReviewsForTests();
  vi.clearAllMocks();
  mocks.commitComparisons = null;
  mocks.width.mockReturnValue(0);
  mocks.asleep.mockReturnValue(false);
  window.localStorage.clear();
  client = new QueryClient();
});

describe("ChangesReview", () => {
  it("shows the review under the pane header, from the one shared source", () => {
    const shared = changes();
    mocks.source.mockReturnValue(source(shared));
    renderReview({ revealPath: "src/app.ts" });
    expect(mocks.source).toHaveBeenCalledWith("s1", { active: true });
    expect(screen.getByTestId("changes-review-header")).toHaveTextContent("Updated");
    expect(screen.getByRole("button", { name: "Refresh Diffs" })).toBeInTheDocument();
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(body.changedFiles).toBe(shared);
    expect(body.revealPath).toBe("src/app.ts");
    expect(body.presentation?.tree?.appearance).toBe("reference");
    expect(body.presentation?.treeRowActions).toBe(true);
  });

  it("offers View File for each file, and withdraws ⌘K's file actions while inactive", () => {
    mocks.source.mockReturnValue(source(changes()));
    const { props, rerender } = renderReview();
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    const [view] = body.fileActions!({ path: "src/app.ts", change: "modified" } as never);
    expect(view.label).toBe("View File");
    view.run();
    expect(props.onViewFile).toHaveBeenCalledWith("src/app.ts");
    rerender(<ChangesReview {...props} active={false} />);
    const inactive = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(inactive.fileActionsInPalette).toBe(false);
    expect(inactive.active).toBe(false);
  });

  it("offers comments on a selection only while shown, handing each one on as a fresh request", () => {
    mocks.source.mockReturnValue(source(changes()));
    const onCommentOnFile = vi.fn();
    const { props, rerender } = renderReview({ onCommentOnFile });
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    // Actions only: none of the session's stored comments are shown in the review.
    expect(body.comments).toMatchObject({ storedComments: false, currentComments: [] });
    expect(body.comments!.canCommentOn("src/app.ts")).toBe(true);
    const selection = { start_index: 2, end_index: 7, anchor_content: "hello" };
    body.comments!.onAddComment!("src/app.ts", selection, "# hello\n");
    body.comments!.onAddComment!("src/app.ts", selection, "# hello\n");
    expect(onCommentOnFile).toHaveBeenCalledTimes(2);
    const [first, second] = onCommentOnFile.mock.calls.map(([request]) => request);
    expect(first).toEqual({
      id: expect.any(String),
      path: "src/app.ts",
      ...selection,
      text: "# hello\n",
    });
    expect(second.id).not.toBe(first.id);
    rerender(<ChangesReview {...props} active={false} />);
    const inactive = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(inactive.comments).toBeUndefined();
  });

  it("offers no comments while the review's head is a commit, and does again on the working copy", () => {
    const typed = (headIsWorkingCopy: boolean) =>
      changes({
        comparison: {
          base: "branch-point",
          status: null,
          comparison: {
            id: "c1",
            ref: "v1",
            baseOid: "a".repeat(40),
            headOid: "b".repeat(40),
            refOid: "a".repeat(40),
            entries: [],
            unrepresentableNames: 0,
            listedAt: 1,
            revisions: {
              spec: { base: "v1", ...(headIsWorkingCopy ? {} : { head: "v2" }) },
              baseDisplay: "v1",
              headDisplay: headIsWorkingCopy ? "" : "v2",
              headIsWorkingCopy,
            },
          },
        } as WorkingCopyChanges["comparison"],
      });
    const onCommentOnFile = vi.fn();
    mocks.source.mockReturnValue(source(typed(true)));
    const { props, rerender } = renderReview({ onCommentOnFile });
    expect(mocks.body.mock.calls.at(-1)![0].comments).toBeDefined();
    // Switching to a commit head withdraws the actions, a pending selection's with them.
    mocks.source.mockReturnValue(source(typed(false)));
    rerender(<ChangesReview {...props} />);
    expect(mocks.body.mock.calls.at(-1)![0].comments).toBeUndefined();
  });

  it("offers no comments where nothing can take them", () => {
    mocks.source.mockReturnValue(source(changes()));
    renderReview();
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(body.comments).toBeUndefined();
  });

  it("keeps the comparison in the header while a comparison can't be shown, with its recoveries", () => {
    const shared = changes({
      comparison: {
        base: "unavailable",
        message: "No branch point with origin/main.",
        retry: true,
      },
    });
    const { view, actions } = source(shared, { intent: "branch-point" });
    mocks.source.mockReturnValue({ changes: shared, view, actions });
    renderReview();
    expect(screen.queryByTestId("review-body")).toBeNull();
    const trigger = screen.getByRole("button", { name: "Change compared revisions" });
    expect(trigger).toHaveAttribute("data-invalid", "true");
    // What is compared sits in the header, above the toolbar that shows how.
    expect(screen.getByTestId("changes-review-header")).toContainElement(trigger);
    expect(screen.getByText("Comparison Unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(actions.retryBranchPoint).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Edit Comparison" }));
    expect(screen.getByRole("combobox", { name: "Base revision" })).toBeInTheDocument();
  });

  it("says when the changes couldn't load, and while they're comparing, with the comparison kept", () => {
    const failed = changes({ data: undefined, isError: true, error: new Error("Server said no.") });
    mocks.source.mockReturnValue(source(failed));
    const { unmount } = renderReview();
    expect(screen.getByText("Diff Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Server said no.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change compared revisions" })).toBeInTheDocument();
    unmount();

    mocks.source.mockReturnValue(
      source(
        changes({
          data: undefined,
          comparison: { base: "pending", message: "Comparing with the branch point…" },
        }),
      ),
    );
    renderReview();
    expect(screen.getByText("Comparing with the branch point…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change compared revisions" })).toBeInTheDocument();
  });

  it("offers Resume while the sandbox sleeps, never calling an unread working copy clean", () => {
    mocks.asleep.mockReturnValue(true);
    const unread = { available: true, data: [] };
    mocks.source.mockReturnValue(source(changes({ data: unread, readable: false })));
    renderReview();
    const notice = screen.getByTestId("sandbox-resume-notice");
    expect(notice).toHaveAttribute("data-status", "asleep");
    expect(notice).toHaveTextContent("Resume it to see what changed.");
    fireEvent.click(screen.getByRole("button", { name: "Resume sandbox" }));
    expect(mocks.resume).toHaveBeenCalledTimes(1);
    // No empty review to read as clean, and no promise of a reconnect on its own.
    expect(screen.queryByTestId("review-body")).not.toBeInTheDocument();
    expect(screen.queryByTestId("changes-review-status")).not.toBeInTheDocument();
    expect(screen.queryByText("0 files")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change compared revisions" })).toBeInTheDocument();
  });

  it("keeps a list cached before the sandbox slept, under the Resume banner", () => {
    mocks.asleep.mockReturnValue(true);
    mocks.source.mockReturnValue(source(changes({ readable: false })));
    renderReview();
    expect(screen.getByTestId("sandbox-resume-notice")).toHaveAttribute("data-status", "asleep");
    expect(screen.getByRole("button", { name: "Resume sandbox" })).toBeInTheDocument();
    expect(screen.getByTestId("review-body")).toBeInTheDocument();
  });

  it("offers Resume for a linked file the cached list doesn't have", () => {
    mocks.asleep.mockReturnValue(true);
    mocks.source.mockReturnValue(source(changes({ readable: false })));
    renderReview({ revealPath: "docs/gone.md" });
    expect(screen.getByText("No Changes to gone.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume sandbox" }));
    expect(mocks.resume).toHaveBeenCalledTimes(1);
  });

  it("still says it's reconnecting to a machine that isn't asleep", () => {
    mocks.source.mockReturnValue(source(changes({ data: undefined, readable: false })));
    renderReview();
    expect(screen.queryByTestId("sandbox-resume-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("changes-review-status")).toHaveTextContent("Reconnecting");
  });

  it("says a file isn't changed, offering all changes or the file itself", () => {
    mocks.source.mockReturnValue(source(changes()));
    const { props } = renderReview({ revealPath: "docs/gone.md" });
    expect(screen.getByText("No Changes to gone.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show All Changes" }));
    expect(props.onShowAllChanges).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "View File" }));
    expect(props.onViewFile).toHaveBeenCalledWith("docs/gone.md");
  });

  it("leaves a linked base and head it can't compare for the session's own comparison", () => {
    mocks.source.mockReturnValue(source(changes()));
    const { props } = renderReview({ requestedRange: { base: "v1.2", head: "Working Copy" } });
    expect(screen.getByText(/Comparing “v1.2” with “Working Copy”/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show Default Comparison" }));
    expect(props.onShowDefaultComparison).toHaveBeenCalledOnce();
  });

  it("publishes the files it shows for links, none for a linked range it can't compare", () => {
    mocks.source.mockReturnValue(source(changes()));
    const { rerender, props } = renderReview();
    expect(shownReview("s1")).toEqual({ state: "listed", paths: ["src/app.ts"] });

    // The session's own list isn't this comparison's, so a link to its file opens Files.
    rerender(<ChangesReview {...props} requestedRange={{ base: "v1.2", head: "Working Copy" }} />);
    expect(shownReview("s1")).toEqual({ state: "unavailable" });

    rerender(<ChangesReview {...props} active={false} />);
    expect(shownReview("s1")).toBeNull();
  });

  it("retires a citation once the reader scrolls, so a remount doesn't replay it", () => {
    mocks.source.mockReturnValue(source(changes()));
    const position = { line: 100 };
    renderReview({ revealPath: "src/app.ts", revealPosition: position });
    expect(isFilePositionPending(position)).toBe(true);
    fireEvent.wheel(screen.getByTestId("changes-review"));
    expect(isFilePositionPending(position)).toBe(false);
  });

  it("opens find for ⌘F after a click on the review's text, and not once the reader leaves", () => {
    mocks.source.mockReturnValue(source(changes()));
    renderReview();
    const outside = document.body.appendChild(document.createElement("div"));
    const findOpen = () =>
      (mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps).findOpen;

    // Diff text takes no focus, so only the click says the reader works here.
    fireEvent.mouseDown(outside);
    fireEvent.mouseDown(screen.getByTestId("review-body"));
    act(() => openFindForActiveSurface());
    expect(findOpen()).toBe(true);
    act(() => mocks.body.mock.calls.at(-1)![0].onFindClose());

    fireEvent.mouseDown(outside);
    act(() => openFindForActiveSurface());
    expect(findOpen()).toBe(false);
    outside.remove();
  });

  it("forgets cached diff text when access is lost, even with no review shown", () => {
    client.setQueryData(["file-diff", "s1", "src/app.ts"], { before: "a", after: "b" });
    const cancel = vi.spyOn(client, "cancelQueries");
    mocks.source.mockReturnValue(source(changes()));
    const { rerender, props } = renderReview();
    mocks.source.mockReturnValue(source(changes({ permitted: false })));
    rerender(<ChangesReview {...props} />);
    expect(screen.queryByTestId("review-body")).toBeNull();
    expect(screen.getByText("You don't have access to this session's files.")).toBeInTheDocument();
    expect(cancel).toHaveBeenCalledWith({ queryKey: ["file-diff", "s1"] });
    expect(client.getQueryData(["file-diff", "s1", "src/app.ts"])).toBeUndefined();
    // Nothing brings it back while access stays lost.
    rerender(<ChangesReview {...props} />);
    expect(client.getQueryData(["file-diff", "s1", "src/app.ts"])).toBeUndefined();
  });

  it("saves only its layout, whitespace and wrapping, keeping the file viewer's choices", () => {
    writeFileViewPreferences({
      ...readFileViewPreferences(),
      diffActive: true,
      previewableViewMode: "source",
    });
    mocks.source.mockReturnValue(source(changes()));
    renderReview();
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    // The file viewer changed its own choice since this review read the preferences.
    writeFileViewPreferences({ ...readFileViewPreferences(), previewableViewMode: "preview" });
    act(() => body.onLayoutChange("split"));
    act(() => body.onHideWhitespaceChange(true));
    expect(readFileViewPreferences()).toMatchObject({
      diffActive: true,
      previewableViewMode: "preview",
      diffLayout: "split",
      hideWhitespace: true,
    });
  });
});

const SHOWN_COMMIT = "c".repeat(40);
const NEXT_COMMIT = "d".repeat(40);

/** ⌘K's Show a Commit… for session s1, if registered. */
const showCommitCommand = () =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .find((command) => command.id === "show-a-commit:s1");

/** A review whose session can compare revisions and lists these commits once asked. */
function reviewWithCommits(listed = [commitRow(SHOWN_COMMIT, "Fix the build")]) {
  mocks.fetchBranchCommits.mockResolvedValue({
    kind: "listed",
    commits: listed,
    truncated: false,
    baseDisplay: "origin/main",
  });
  const shared = changes({
    revisionSupport: { kind: "available" },
    workspace: { hostId: "host_1", root: "/repo" },
  });
  const built = source(shared);
  mocks.source.mockReturnValue(built);
  // The open commit's own changes, listed once its owner compares it.
  mocks.commitChanges.mockImplementation((spec) =>
    changes({
      custom: spec,
      comparison: spec
        ? {
            base: "branch-point",
            status: null,
            comparison: {
              id: `cmp_${spec.head}`,
              ref: "",
              baseOid: "",
              headOid: spec.head,
              refOid: "",
              entries: [],
              unrepresentableNames: 0,
              listedAt: 1,
            },
          }
        : HEAD,
    }),
  );
  return built;
}

/** The last props each mounted review body was given: the session's, then the open commit's. */
function bodies() {
  const calls = mocks.body.mock.calls.map(([props]) => props as WorkingCopyDiffReviewBodyProps);
  return {
    changes: calls.findLast((props) => props.reviewScope === undefined),
    commit: calls.findLast((props) => props.reviewScope !== undefined),
  };
}

/** ⌘K's commands under `heading`, by label. */
function paletteCommands(heading: string) {
  return Object.values(usePaletteCommandsStore.getState().groups)
    .filter((group) => group.heading === heading)
    .flatMap((group) => group.commands);
}

function commitRow(oid: string, subject: string, root = false) {
  return { oid, subject, authorName: "Ada", authoredAtMs: null, root };
}

// Commits names its count too, once the commits are listed.
const tab = (name: "Commits" | "Changes") =>
  screen.getByRole("tab", { name: name === "Commits" ? /^Commits/ : name });

describe("the Commits and Changes tabs", () => {
  it("closes the comparison picker and drops what it showed and held once access is lost", async () => {
    reviewWithCommits();
    client.setQueryData(["repository-refs", "s1", "host_1", "/repo"], {
      refs: ["release/next"],
      truncated: false,
      defaultBranchRef: "origin/main",
    });
    const { rerender, props } = renderReview();
    const trigger = screen.getByRole("button", { name: "Change compared revisions" });
    const branchTitle = trigger.getAttribute("title");
    await userEvent.click(trigger);
    const base = screen.getByRole("combobox", { name: "Base revision" });
    await userEvent.clear(base);
    await userEvent.type(base, "typed-base");

    const revoked = changes({
      permitted: false,
      revisionSupport: { kind: "available" },
      workspace: { hostId: "host_1", root: "/repo" },
    });
    mocks.source.mockReturnValue(source(revoked));
    rerender(<ChangesReview {...props} />);
    expect(screen.queryByRole("button", { name: "Change compared revisions" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Base revision" })).toBeNull();
    expect(screen.queryByDisplayValue("typed-base")).toBeNull();
    expect(screen.queryByText("release/next")).toBeNull();
    if (branchTitle) expect(document.querySelector(`[title="${branchTitle}"]`)).toBeNull();

    // Access back: the picker starts over, without the typed text.
    reviewWithCommits();
    rerender(<ChangesReview {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "Change compared revisions" }));
    expect(screen.queryByDisplayValue("typed-base")).toBeNull();
  });

  it("hides Commits and forgets the branch's commits and names once access is lost", async () => {
    reviewWithCommits();
    client.setQueryData(["repository-refs", "s1", "host_1", "/repo"], {
      refs: ["main"],
      truncated: false,
      defaultBranchRef: "origin/main",
    });
    const { rerender, props } = renderReview();
    await userEvent.click(tab("Commits"));
    expect(await screen.findByRole("option", { name: /Fix the build/ })).toBeVisible();
    const listings = mocks.fetchBranchCommits.mock.calls.length;

    mocks.source.mockReturnValue(
      source(
        changes({
          permitted: false,
          revisionSupport: { kind: "available" },
          workspace: { hostId: "host_1", root: "/repo" },
        }),
      ),
    );
    rerender(<ChangesReview {...props} />);
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
    expect(screen.queryByText(/Fix the build/)).toBeNull();
    // A disabled observer may keep an empty entry; no listed data survives.
    const cached = (key: string) =>
      client.getQueriesData({ queryKey: [key, "s1"] }).filter(([, data]) => data !== undefined);
    expect(cached("branch-commits")).toEqual([]);
    expect(cached("repository-refs")).toEqual([]);
    rerender(<ChangesReview {...props} />);
    expect(mocks.fetchBranchCommits).toHaveBeenCalledTimes(listings);
  });

  it("sit in the header before Updated, start on Changes, and count the branch's commits", async () => {
    reviewWithCommits([commitRow(SHOWN_COMMIT, "Fix the build"), commitRow(NEXT_COMMIT, "Add")]);
    renderReview();
    const header = screen.getByTestId("changes-review-header");
    const tabs = screen.getByRole("tablist", { name: "Diffs" });
    expect(header).toContainElement(tabs);
    const updated = screen.getByText("Updated", { exact: false });
    expect(tabs.compareDocumentPosition(updated) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("review-body")).toBeVisible();
    // Listed from Changes too, so the tab can say how many there are.
    expect(await screen.findByText("2", { selector: "[data-slot=badge]" })).toBeVisible();
    expect(tab("Commits")).toContainElement(
      screen.getByText("2", { selector: "[data-slot=badge]" }),
    );
    expect(mocks.fetchBranchCommits).toHaveBeenCalledWith("s1", expect.anything());

    await userEvent.click(tab("Commits"));
    expect(await screen.findByRole("option", { name: /Fix the build/ })).toBeVisible();
    // The review stays mounted behind Commits, but isn't the active surface.
    const behind = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(behind.active).toBe(false);
    expect(behind.fileActionsInPalette).toBe(false);
  });

  it("opens a picked commit in Commits, keeping the comparison, with focus on its title", async () => {
    const { actions } = reviewWithCommits();
    renderReview();
    tab("Changes").focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(tab("Commits")).toHaveFocus();
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    await screen.findByRole("option", { name: /Fix the build/ });
    // Past the header's Refresh, Tab reaches the list's filter.
    const filter = screen.getByRole("combobox", { name: "Filter commits" });
    for (let press = 0; press < 4 && document.activeElement !== filter; press++) {
      // oxlint-disable-next-line no-await-in-loop -- each key press after the one before
      await userEvent.tab();
    }
    expect(filter).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(actions.compareRevisions).not.toHaveBeenCalled();
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Fix the build" })).toHaveFocus();
    expect(screen.queryByTestId("changes-review-commits")).toBeNull();
    expect(mocks.commitComparisons!.read("s1", null!)).toEqual({
      base: `${SHOWN_COMMIT}~1`,
      head: SHOWN_COMMIT,
    });
  });

  it("compares a root commit with the empty tree, since it has no parent", async () => {
    reviewWithCommits([commitRow(SHOWN_COMMIT, "Start the repository", true)]);
    renderReview();
    await userEvent.click(tab("Commits"));
    await userEvent.click(await screen.findByRole("option", { name: /Start the repository/ }));
    expect(mocks.commitComparisons!.read("s1", null!)).toEqual({
      base: "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
      head: SHOWN_COMMIT,
    });
  });

  it("goes back to Changes for a new file or line to show, keeping the comparison", async () => {
    const { actions } = reviewWithCommits();
    const { props, rerender } = renderReview({ revealPath: "src/app.ts" });
    await userEvent.click(tab("Commits"));
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    // The same request again changes nothing.
    rerender(<ChangesReview {...props} revealPath="src/app.ts" />);
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    rerender(<ChangesReview {...props} revealPath="src/app.ts" revealPosition={{ line: 4 }} />);
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    const body = mocks.body.mock.calls.at(-1)![0] as WorkingCopyDiffReviewBodyProps;
    expect(body.revealPath).toBe("src/app.ts");
    expect(body.position).toEqual({ line: 4 });
    expect(actions.compareRevisions).not.toHaveBeenCalled();
  });

  it("falls back to Changes, with no tabs, once revisions can't be compared", async () => {
    reviewWithCommits();
    const { props, rerender } = renderReview();
    await userEvent.click(tab("Commits"));
    mocks.source.mockReturnValue(source(changes()));
    rerender(<ChangesReview {...props} />);
    expect(screen.queryByRole("tablist", { name: "Diffs" })).toBeNull();
    expect(screen.queryByTestId("changes-review-commits")).toBeNull();
    expect(screen.getByTestId("review-body")).toBeVisible();
  });

  it("shows Changes for a comparison chosen in the picker while Commits is shown", async () => {
    const { actions } = reviewWithCommits();
    renderReview();
    const picker = screen.getByRole("button", { name: "Change compared revisions" });
    await userEvent.click(tab("Commits"));
    await userEvent.click(picker);
    await userEvent.click(screen.getByRole("button", { name: /^Uncommitted changes/ }));
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("changes-review-commits")).toBeNull();

    await userEvent.click(tab("Commits"));
    await userEvent.click(picker);
    const base = screen.getByRole("combobox", { name: "Base revision" });
    await userEvent.clear(base);
    await userEvent.type(base, "v1");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(actions.compareRevisions).toHaveBeenCalledWith({ base: "v1", head: "" });
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("review-body")).toBeVisible();
  });

  it("goes back to Changes when the comparison changes elsewhere, as from ⌘K", async () => {
    const built = reviewWithCommits();
    const { props, rerender } = renderReview();
    await userEvent.click(tab("Commits"));
    // A new answer for the same comparison keeps Commits.
    mocks.source.mockReturnValue(source(built.changes));
    rerender(<ChangesReview {...props} />);
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    mocks.source.mockReturnValue(source(built.changes, { intent: "automatic" }));
    rerender(<ChangesReview {...props} />);
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");

    await userEvent.click(tab("Commits"));
    mocks.source.mockReturnValue(
      source({ ...built.changes, custom: { base: "v1", head: "" } }, { intent: "automatic" }),
    );
    rerender(<ChangesReview {...props} />);
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
  });

  it("lists the commits again when Commits is shown after the listing went stale", async () => {
    reviewWithCommits();
    renderReview();
    await userEvent.click(tab("Commits"));
    await screen.findByRole("option", { name: /Fix the build/ });
    await userEvent.click(tab("Changes"));
    mocks.fetchBranchCommits.mockResolvedValue({
      kind: "listed",
      commits: [commitRow(NEXT_COMMIT, "A new commit"), commitRow(SHOWN_COMMIT, "Fix the build")],
      truncated: false,
      baseDisplay: "origin/main",
    });
    const now = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(now + 31_000);
    try {
      await userEvent.click(tab("Commits"));
      expect(await screen.findByRole("option", { name: /A new commit/ })).toBeVisible();
      expect(tab("Commits")).toContainElement(
        screen.getByText("2", { selector: "[data-slot=badge]" }),
      );
    } finally {
      clock.mockRestore();
    }
  });

  it("lists the commits again on Refresh Diffs, adding new ones and dropping rebased ones", async () => {
    const built = reviewWithCommits();
    // Refresh Diffs runs the session's Refresh Changes, which says its files may have changed.
    mocks.source.mockReturnValue({
      ...built,
      view: { ...built.view, refresh: () => invalidateWorkingCopyChanges(client, "s1") },
    });
    renderReview();
    expect(await screen.findByText("1", { selector: "[data-slot=badge]" })).toBeVisible();
    const listing = (commits: ReturnType<typeof commitRow>[]) =>
      mocks.fetchBranchCommits.mockResolvedValue({
        kind: "listed",
        commits,
        truncated: false,
        baseDisplay: "origin/main",
      });

    listing([commitRow(NEXT_COMMIT, "A new commit"), commitRow(SHOWN_COMMIT, "Fix the build")]);
    await userEvent.click(screen.getByRole("button", { name: "Refresh Diffs" }));
    expect(await screen.findByText("2", { selector: "[data-slot=badge]" })).toBeVisible();

    listing([commitRow(NEXT_COMMIT, "A new commit")]);
    await userEvent.click(screen.getByRole("button", { name: "Refresh Diffs" }));
    expect(await screen.findByText("1", { selector: "[data-slot=badge]" })).toBeVisible();
    await userEvent.click(tab("Commits"));
    expect(await screen.findByRole("option", { name: /A new commit/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /Fix the build/ })).toBeNull();
  });

  it("keeps the choice out of the URL", async () => {
    reviewWithCommits();
    const before = window.location.href;
    renderReview();
    await userEvent.click(tab("Commits"));
    await userEvent.click(tab("Changes"));
    expect(window.location.href).toBe(before);
  });
});

describe("Show a Commit… in ⌘K", () => {
  it("opens Commits on its filter, and again on a repeat", async () => {
    reviewWithCommits();
    renderReview();
    const command = showCommitCommand();
    expect(command?.availability).toEqual({ available: true });
    act(() => command!.run());
    expect(tab("Commits")).toHaveAttribute("aria-selected", "true");
    const filter = await screen.findByRole("combobox", { name: "Filter commits" });
    expect(filter).toHaveFocus();
    tab("Commits").focus();
    act(() => showCommitCommand()!.run());
    expect(filter).toHaveFocus();
  });

  it("lands on the list while it loads, then on the filter once commits arrive", async () => {
    let answer: (value: unknown) => void = () => {};
    reviewWithCommits();
    mocks.fetchBranchCommits.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    renderReview();
    act(() => showCommitCommand()!.run());
    expect(await screen.findByText("Listing this branch's commits…")).toBeVisible();
    expect(screen.getByTestId("changes-review-commits")).toHaveFocus();
    await act(async () =>
      answer({
        kind: "listed",
        commits: [commitRow(SHOWN_COMMIT, "Fix the build")],
        truncated: false,
        baseDisplay: "origin/main",
      }),
    );
    expect(await screen.findByRole("combobox", { name: "Filter commits" })).toHaveFocus();
  });

  it("leaves focus where the reader went when the commits arrive late", async () => {
    let answer: (value: unknown) => void = () => {};
    reviewWithCommits();
    mocks.fetchBranchCommits.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    renderReview();
    act(() => showCommitCommand()!.run());
    await screen.findByText("Listing this branch's commits…");
    // The reader moves on to Refresh, still on Commits.
    const refresh = screen.getByRole("button", { name: "Refresh Diffs" });
    refresh.focus();
    await act(async () =>
      answer({
        kind: "listed",
        commits: [commitRow(SHOWN_COMMIT, "Fix the build")],
        truncated: false,
        baseDisplay: "origin/main",
      }),
    );
    expect(await screen.findByRole("option", { name: /Fix the build/ })).toBeVisible();
    expect(refresh).toHaveFocus();
  });

  it("says why where revisions can't be compared, and is gone with the review", () => {
    mocks.source.mockReturnValue(source(changes()));
    const { unmount } = renderReview();
    expect(showCommitCommand()?.availability).toEqual({ available: false, reason: "Update" });
    unmount();
    expect(showCommitCommand()).toBeUndefined();
    renderReview({ active: false });
    expect(showCommitCommand()).toBeUndefined();
  });
});

describe("an open commit", () => {
  async function openFirstCommit() {
    const built = reviewWithCommits([
      commitRow(SHOWN_COMMIT, "Fix the build"),
      commitRow(NEXT_COMMIT, "Add the parser"),
    ]);
    const rendered = renderReview();
    await userEvent.click(tab("Commits"));
    await userEvent.click(await screen.findByRole("option", { name: /Fix the build/ }));
    return { ...built, ...rendered };
  }

  it("names it in a toolbar over its own changes, leaving the session's comparison", async () => {
    const picker = () => screen.getByRole("button", { name: "Change compared revisions" });
    reviewWithCommits();
    const { unmount } = renderReview();
    const comparisonBefore = picker().textContent;
    unmount();

    const { actions } = await openFirstCommit();
    const toolbar = screen.getByTestId("changes-review-commit-toolbar");
    expect(toolbar).toHaveTextContent("Fix the build");
    expect(toolbar).toHaveTextContent(`Ada·${SHOWN_COMMIT.slice(0, 7)}`);
    expect(screen.getByTestId("changes-review-commit-position")).toHaveTextContent("1 of 2");
    expect(screen.getByRole("button", { name: "← Commits".slice(2) })).toBeVisible();
    expect(picker().textContent).toBe(comparisonBefore);
    expect(actions.compareRevisions).not.toHaveBeenCalled();

    const { changes: session, commit } = bodies();
    expect(commit?.changedFiles.custom).toEqual({ base: `${SHOWN_COMMIT}~1`, head: SHOWN_COMMIT });
    expect(commit?.reviewScope).toBe(`commit:s1:${SHOWN_COMMIT}`);
    expect(commit?.active).toBe(true);
    expect(commit?.comments).toBeUndefined();
    expect(commit?.presentation?.messages?.empty?.message).toBe(
      "This commit doesn't change any files.",
    );
    // The session's review stays behind Changes, inactive.
    expect(session?.active).toBe(false);
  });

  it("steps through the list with ↑ and ↓, saying why at either end", async () => {
    await openFirstCommit();
    const up = screen.getByRole("button", { name: /^Previous commit/ });
    expect(up).toBeDisabled();
    expect(up).toHaveAccessibleName("Previous commit: This is the newest commit.");

    await userEvent.click(screen.getByRole("button", { name: "Next commit" }));
    expect(screen.getByRole("heading", { name: "Add the parser" })).toBeVisible();
    expect(screen.getByTestId("changes-review-commit-position")).toHaveTextContent("2 of 2");
    expect(mocks.commitComparisons!.read("s1", null!)?.head).toBe(NEXT_COMMIT);
    expect(bodies().commit?.reviewScope).toBe(`commit:s1:${NEXT_COMMIT}`);
    // ↓ can't go further, so focus moves to ↑.
    expect(screen.getByRole("button", { name: /^Next commit/ })).toHaveAccessibleName(
      "Next commit: This is the oldest commit.",
    );
    expect(screen.getByRole("button", { name: "Previous commit" })).toHaveFocus();

    // The arrow keys step too while focus is in the toolbar.
    await userEvent.keyboard("{ArrowUp}");
    expect(screen.getByRole("heading", { name: "Fix the build" })).toBeVisible();
    await userEvent.keyboard("{ArrowUp}");
    expect(screen.getByRole("heading", { name: "Fix the build" })).toBeVisible();
  });

  it("goes back to the list with ← Commits, marking the commit it showed", async () => {
    await openFirstCommit();
    await userEvent.click(screen.getByRole("button", { name: "Commits" }));
    expect(screen.queryByTestId("changes-review-commit-toolbar")).toBeNull();
    const row = await screen.findByRole("option", { name: /Fix the build/ });
    expect(row).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("combobox", { name: "Filter commits" })).toHaveFocus();
  });

  it("stays open behind Changes, and a new file to show only switches tabs", async () => {
    const { props, rerender } = await openFirstCommit();
    await userEvent.click(tab("Changes"));
    expect(bodies().changes?.active).toBe(true);
    rerender(<ChangesReview {...props} revealPath="src/app.ts" />);
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    await userEvent.click(tab("Commits"));
    expect(screen.getByRole("heading", { name: "Fix the build" })).toBeVisible();
  });

  it("offers the same steps in ⌘K while it's shown, under Changes", async () => {
    await openFirstCommit();
    const labels = paletteCommands("Changes").map((command) => command.label);
    expect(labels).toEqual(
      expect.arrayContaining([
        "Go to the previous commit",
        "Go to the next commit",
        "Show all commits",
        "Show all changes",
      ]),
    );
    const next = paletteCommands("Changes").find((c) => c.label === "Go to the next commit")!;
    expect(next.keywords).toEqual(expect.arrayContaining(["older", "branch"]));
    act(() => next.run());
    expect(screen.getByRole("heading", { name: "Add the parser" })).toBeVisible();

    const changesCommand = paletteCommands("Changes").find((c) => c.label === "Show all changes")!;
    act(() => changesCommand.run());
    expect(tab("Changes")).toHaveAttribute("aria-selected", "true");
    // Gone once the commit isn't on screen.
    expect(paletteCommands("Changes").map((command) => command.label)).not.toContain(
      "Go to the next commit",
    );
  });

  it("says when its changes can't be read, and tries again", async () => {
    reviewWithCommits();
    mocks.commitChanges.mockImplementation((spec) =>
      changes({
        custom: spec,
        data: undefined,
        comparison: { base: "unavailable", message: "No", retry: false },
      }),
    );
    renderReview();
    await userEvent.click(tab("Commits"));
    await userEvent.click(await screen.findByRole("option", { name: /Fix the build/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("Commit Changes Unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(mocks.commitOwner.retry).toHaveBeenCalledWith("s1");
    expect(bodies().commit).toBeUndefined();
  });

  it("waits for its own listing rather than show the previous commit's", async () => {
    reviewWithCommits();
    mocks.commitChanges.mockImplementation(() => changes({ custom: { base: "x~1", head: "x" } }));
    renderReview();
    await userEvent.click(tab("Commits"));
    await userEvent.click(await screen.findByRole("option", { name: /Fix the build/ }));
    expect(screen.getByTestId("changes-review-commit-message")).toHaveTextContent(
      "Loading commit changes…",
    );
    expect(bodies().commit).toBeUndefined();
  });
});

describe("the Commits tab's count", () => {
  it("says a cut-off list is a lower bound", async () => {
    reviewWithCommits();
    mocks.fetchBranchCommits.mockResolvedValue({
      kind: "listed",
      commits: [commitRow(SHOWN_COMMIT, "Fix the build")],
      truncated: true,
      baseDisplay: "origin/main",
    });
    renderReview();
    const count = await screen.findByText("1+", { selector: "[data-slot=badge]" });
    expect(tab("Commits")).toContainElement(count);
  });

  it("stays beside the icon where labels give way, and in the folded picker", async () => {
    reviewWithCommits();
    const { rerender, props } = renderReview();
    const count = await screen.findByText("1", { selector: "[data-slot=badge]" });
    const commits = tab("Commits");
    expect(commits).toContainElement(count);
    // Narrow panes hide only the label, which stays the tab's name; the count stays beside the icon.
    expect(commits.querySelector("[data-section-label]")).not.toContainElement(count);

    mocks.width.mockReturnValue(300);
    rerender(<ChangesReview {...props} />);
    await userEvent.click(screen.getByRole("combobox", { name: "Diffs section" }));
    const option = await screen.findByRole("option", { name: /^Commits/ });
    expect(option).toHaveTextContent("1");
  });
});
