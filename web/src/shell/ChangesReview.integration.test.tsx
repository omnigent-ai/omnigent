// Changes review with the real working-copy review body mounted: only the
// renderer (DiffReview) and its toolbar are stand-ins, so the body's own rows,
// loader and read guards run as in the app.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DiffReviewProps } from "@/components/diff-review/DiffReview";
import type { DiffReviewToolbarProps } from "@/components/diff-review/DiffReviewToolbar";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkingCopyChanges } from "@/hooks/useWorkingCopyChanges";
import { comparisonChoiceOf } from "@/lib/comparisonChoice";
import type { ComparisonAvailability } from "@/shell/useVisitComparison";
import { localComparisonView, type LocalComparisonInput } from "@/lib/localComparison";
import {
  type BranchPointComparison,
  effectiveComparison,
  type EffectiveComparison,
} from "@/lib/workingCopyComparison";
import type * as FileDiffModule from "@/hooks/useFileDiff";
import {
  ChangesReview,
  type ChangesReviewProps,
  RANGE_PRESET_REASON,
  requestedRangeView,
} from "./ChangesReview";
import { WorkingCopyDiffReviewBody } from "./WorkingCopyDiffReview";

const mocks = vi.hoisted(() => ({ source: vi.fn(), read: vi.fn() }));
vi.mock("./useLocalComparison", () => ({ useLocalComparison: mocks.source }));
vi.mock("@/hooks/useFileDiff", async (importOriginal) => ({
  ...(await importOriginal<typeof FileDiffModule>()),
  fetchFileDiff: mocks.read,
}));
vi.mock("@/components/diff-review/DiffReviewToolbar", () => ({
  DiffReviewToolbar: ({ comparison }: DiffReviewToolbarProps) => <div>{comparison}</div>,
}));
vi.mock("@/components/diff-review/DiffReview", () => ({
  DiffReview: (props: DiffReviewProps) => (
    <div data-testid="review">
      {props.presentation?.toolbar?.({} as DiffReviewToolbarProps)}
      <pre data-testid="loaded-text">{JSON.stringify([...props.contents])}</pre>
    </div>
  ),
}));
vi.mock("./fileCommands/useFileCommandFacts", () => ({ useFileCommandEnvironment: () => ({}) }));
vi.mock("./fileCommands/target", () => ({
  useSessionHostId: () => null,
  resolveFileCommandTarget: (path: string) => ({ path }),
}));
vi.mock("./fileCommands/reviewFileActions", () => ({ reviewFileActions: () => [] }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));

const HEAD: EffectiveComparison = { base: "head", note: null, override: false, retry: false };
const STATUS_ROWS =
  '[data-testid="changes-review-status"],[data-testid="working-copy-comparison-status"]';

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
    choices: 0,
    readable: true,
    ...overrides,
  };
}

function source(shared: WorkingCopyChanges, overrides: Partial<LocalComparisonInput> = {}) {
  const automatic = vi.fn();
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
        run: automatic,
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
    actions: {
      refetchHead: vi.fn(),
      retryBranchPoint: vi.fn(),
      showLastCommit: vi.fn(),
      compareAgain: vi.fn(),
      compareRevisions: vi.fn(),
    },
    ...overrides,
  });
  const choice = {
    shown: comparisonChoiceOf(overrides.intent ?? "head", shared.custom),
    settled: shared.workspace !== null,
    availability: (): ComparisonAvailability => "now",
    choose: vi.fn(),
  };
  return { changes: shared, view, automatic, choice };
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

function bodyProps(shared: WorkingCopyChanges) {
  return {
    conversationId: "s1",
    changedFiles: shared,
    base: null,
    revealPath: "src/app.ts",
    sort: "alpha" as const,
    layout: "unified" as const,
    onLayoutChange: () => {},
    wrapLines: false,
    onWrapLinesChange: () => {},
    hideWhitespace: false,
    onHideWhitespaceChange: () => {},
    findOpen: false,
    findFocusRequest: 0,
    onFindClose: () => {},
  };
}

const statusRows = () => [...document.querySelectorAll(STATUS_ROWS)].map((row) => row.textContent);

afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  client = new QueryClient();
});

describe("Changes review's comparison notes", () => {
  it("shows one status row, its own, and keeps an unknown base's wording neutral", async () => {
    mocks.read.mockResolvedValue({
      path: "src/app.ts",
      before: "a\n",
      after: "b\n",
      requestedAt: Date.now(),
    });
    const shared = changes({
      comparison: {
        base: "head",
        note: "Showing uncommitted changes: offline.",
        override: false,
        retry: true,
      },
    });
    mocks.source.mockReturnValue(source(shared, { headIsKnown: false }));
    renderReview();
    await screen.findByTestId("review");
    expect(statusRows()).toEqual(["Showing the changes the host reports: offline.Try Again"]);
  });

  it("leaves the file viewer's review its own status row", async () => {
    mocks.read.mockResolvedValue({
      path: "src/app.ts",
      before: "a\n",
      after: "b\n",
      requestedAt: Date.now(),
    });
    const shared = changes({
      comparison: {
        base: "head",
        note: "Showing uncommitted changes.",
        override: true,
        retry: false,
      },
    });
    render(<WorkingCopyDiffReviewBody {...bodyProps(shared)} />, { wrapper });
    await screen.findByTestId("review");
    expect(statusRows()).toEqual([
      expect.stringContaining("Showing uncommitted changes for this session."),
    ]);
  });

  it("says once, under its toolbar, how many files the branch-point listing left out", async () => {
    const comparison: BranchPointComparison = {
      id: "cmp_1",
      ref: "origin/main",
      baseOid: "a".repeat(40),
      headOid: "b".repeat(40),
      refOid: "c".repeat(40),
      entries: [],
      unrepresentableNames: 2,
      listedAt: 1,
    };
    const shared = changes({ comparison: { base: "branch-point", comparison, status: null } });
    mocks.source.mockReturnValue(source(shared));
    renderReview();
    await screen.findByTestId("review");
    expect(screen.getAllByText("2 files with names that can't be shown")).toHaveLength(1);
    expect(screen.getByTestId("changes-review-left-out")).toBeInTheDocument();
  });
});

describe("why Automatic shows the last commit", () => {
  it("explains a definitive fallback in the comparison popover", async () => {
    mocks.read.mockResolvedValue({
      path: "src/app.ts",
      before: "a\n",
      after: "b\n",
      requestedAt: Date.now(),
    });
    // A real definitive fallback: two repositories, so no branch point; Automatic shows HEAD.
    const comparison = effectiveComparison({
      intent: "automatic",
      support: { kind: "available" },
      multipleRepositories: true,
      established: null,
      problem: null,
      connectTimedOut: false,
      headOverride: false,
    });
    expect(comparison).toMatchObject({ base: "head", retry: false, override: false });
    const shared = changes({ comparison });
    mocks.source.mockReturnValue(source(shared, { intent: "automatic" }));
    renderReview();
    await screen.findByTestId("review");
    expect(statusRows()).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Change compared revisions" }));
    expect(screen.getByTestId("changes-review-fallback-reason")).toHaveTextContent(
      "Branch point works for a single Git repository.",
    );
  });
});

describe("a linked base and head this view can't compare", () => {
  it("names them, marked invalid, and Reset leaves them for the session's comparison", () => {
    const data = source(changes());
    mocks.source.mockReturnValue(data);
    const { props } = renderReview({ requestedRange: { base: "v1.2", head: "v2" } });
    const trigger = screen.getByRole("button", { name: "Change compared revisions" });
    expect(trigger).toHaveTextContent("v1.2…v2");
    expect(trigger).toHaveAttribute("data-invalid", "true");
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "Compared revisions" })).toHaveTextContent(
      "Comparing “v1.2” with “v2” isn't available here yet.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(props.onShowDefaultComparison).toHaveBeenCalledOnce();
    expect(data.automatic).not.toHaveBeenCalled();
  });
});

describe("a linked range beside an unrelated session comparison", () => {
  it("inherits no reconnecting status, recovery or file count from an offline HEAD", () => {
    mocks.source.mockReturnValue(source(changes({ readable: false })));
    renderReview({ requestedRange: { base: "v1.2", head: "v2" } });
    expect(screen.getByText("Comparison Unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("changes-review-status")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show Uncommitted Changes" })).toBeNull();
    expect(screen.queryByTestId("diff-review-file-count")).toBeNull();
  });

  it("inherits nothing from a stale branch point that left files out", () => {
    const comparison: BranchPointComparison = {
      id: "cmp_1",
      ref: "origin/main",
      baseOid: "a".repeat(40),
      headOid: "b".repeat(40),
      refOid: "c".repeat(40),
      entries: [],
      unrepresentableNames: 3,
      listedAt: 1,
    };
    const shared = changes({
      comparison: {
        base: "branch-point",
        comparison,
        status: { kind: "stale", message: "Couldn't refresh: offline" },
      },
    });
    mocks.source.mockReturnValue(source(shared));
    renderReview({ requestedRange: { base: "v1.2", head: "v2" } });
    expect(screen.queryByTestId("changes-review-status")).toBeNull();
    expect(screen.queryByTestId("changes-review-left-out")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try Again" })).toBeNull();
    expect(screen.getByRole("button", { name: "Change compared revisions" })).toHaveTextContent(
      "v1.2…v2",
    );
  });
});

describe("requestedRangeView", () => {
  it("keeps the base choices from changing a linked range, and resets by leaving it", () => {
    const { view } = source(changes());
    const showDefault = vi.fn();
    const range = requestedRangeView(view, { base: "v1.2", head: "v2" }, showDefault);
    expect(range.trigger).toMatchObject({ base: "v1.2", head: "v2", invalid: true });
    expect(range.presets.every((preset) => preset.unavailableReason === RANGE_PRESET_REASON)).toBe(
      true,
    );
    expect(range.reset.disabled).toBe(false);
    range.reset.run();
    expect(showDefault).toHaveBeenCalledOnce();
    expect(range.state).toMatchObject({ kind: "unavailable", canRetry: false });
  });
});

describe("losing access during a read", () => {
  it("never caches or shows an in-flight HEAD read that finishes after access is lost", async () => {
    let finish!: (data: unknown) => void;
    mocks.read.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    mocks.source.mockReturnValue(source(changes()));
    const { props, rerender } = renderReview();
    await waitFor(() => expect(mocks.read).toHaveBeenCalledOnce());
    mocks.source.mockReturnValue(source(changes({ permitted: false, data: undefined })));
    rerender(<ChangesReview {...props} />);
    expect(screen.queryByTestId("review")).toBeNull();
    await act(async () =>
      finish({
        path: "src/app.ts",
        before: "secret old",
        after: "secret new",
        requestedAt: Date.now(),
      }),
    );
    expect(client.getQueriesData({ queryKey: ["file-diff", "s1"] })).toEqual([]);
    expect(screen.queryByText(/secret/)).toBeNull();
  });
});
