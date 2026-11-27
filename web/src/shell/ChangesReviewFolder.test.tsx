import { act, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rememberBrowseLocation, rememberedBrowseLocation } from "./filesPane/browseLocations";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

const WORKTREES = [
  { path: "/repo", branch: "main", is_main: true, detached: false },
  { path: "/repo/.worktrees/feature", branch: "feature", is_main: false, detached: false },
  { path: "/linked/other", branch: "other", is_main: false, detached: false },
];
// A workspace that is a folder of a larger repository, with a worktree of it inside.
const LARGER_REPOSITORY = [
  { path: "/monorepo", branch: "main", is_main: true, detached: false },
  { path: "/monorepo/app/.worktrees/feature", branch: "feature", is_main: false, detached: false },
];

const mocks = vi.hoisted(() => ({
  permissionLevel: 4 as number | null,
  unconfined: true,
  workspace: "/repo",
  worktrees: [] as { path: string; branch: string; is_main: boolean; detached: boolean }[],
  worktreeLookups: [] as (string | null)[],
  /** How the worktree lookup answers: a list, a failure, or not yet. */
  worktreeAnswer: "ok" as "ok" | "unknown" | "pending" | "not_git" | "denied",
  hostId: "host_1" as string | null,
  parentSessionId: null as string | null,
}));
vi.mock("./FileViewerContext", () => ({ useWorkspacePaths: () => ({ root: mocks.workspace }) }));
vi.mock("./fileCommands/target", () => ({ useSessionHostId: () => mocks.hostId }));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({
    session: {
      permissionLevel: mocks.permissionLevel,
      hostId: mocks.hostId,
      parentSessionId: mocks.parentSessionId,
    },
  }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: () => ({
    data: { reachable: { unconfined: mocks.unconfined, roots: [] } },
  }),
}));
vi.mock("@/hooks/useSessionWorktrees", () => ({
  useSessionWorktrees: (_session: string, _host: string, workspace: string | null) => {
    mocks.worktreeLookups.push(workspace);
    if (!workspace) return { data: undefined, isLoading: false };
    if (mocks.worktreeAnswer === "pending") return { data: undefined, isLoading: true };
    if (mocks.worktreeAnswer === "unknown")
      return { data: { status: "unknown" }, isLoading: false };
    if (mocks.worktreeAnswer === "not_git")
      return { data: { status: "not_git" }, isLoading: false };
    if (mocks.worktreeAnswer === "denied") {
      return { data: { status: "unknown", denied: true }, isLoading: false };
    }
    return { data: { status: "ok", worktrees: mocks.worktrees }, isLoading: false };
  },
}));
vi.mock("./WorkspacePickerDialog", () => ({
  WorkspacePickerDialog: ({
    open,
    initialPath,
    onConfirm,
  }: {
    open: boolean;
    initialPath: string;
    onConfirm: (path: string) => void;
  }) =>
    open ? (
      <div data-testid="picker" data-initial={initialPath}>
        <button type="button" onClick={() => onConfirm("/repo/.worktrees/feature")}>
          Pick worktree
        </button>
        <button type="button" onClick={() => onConfirm("/repo")}>
          Pick workspace
        </button>
        <button type="button" onClick={() => onConfirm("/linked/other")}>
          Pick linked worktree
        </button>
      </div>
    ) : null,
}));

import {
  LARGER_REPOSITORY_REASON,
  ReviewFolderName,
  resetReviewFolderListsForTests,
  useReviewFolder,
} from "./ChangesReviewFolder";

const SESSION = "conv_folder";

function Harness({ active = true }: { active?: boolean }) {
  const folder = useReviewFolder(SESSION, active);
  return (
    <>
      <ReviewFolderName folder={folder} />
      <output data-testid="root">{folder.root}</output>
      <output data-testid="repository">{folder.repository ?? "outside"}</output>
      {folder.dialog}
    </>
  );
}

const paletteLabels = () =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .map((command) => command.label);

const paletteCommand = (label: string) =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .find((command) => command.label === label);

beforeEach(() => {
  mocks.permissionLevel = 4;
  mocks.unconfined = true;
  mocks.workspace = "/repo";
  mocks.worktrees = WORKTREES;
  mocks.worktreeLookups = [];
  mocks.worktreeAnswer = "ok";
  mocks.hostId = "host_1";
  mocks.parentSessionId = null;
});
afterEach(() => {
  rememberBrowseLocation(SESSION, null);
  resetReviewFolderListsForTests();
});

describe("useReviewFolder", () => {
  it("chooses a worktree in the folder browser, which Files then shows too, and returns", async () => {
    render(<Harness />);
    expect(screen.getByTestId("root")).toHaveTextContent("/repo");
    await userEvent.click(screen.getByTestId("changes-review-folder"));
    expect(screen.getByTestId("picker")).toHaveAttribute("data-initial", "/repo");
    await userEvent.click(screen.getByRole("button", { name: "Pick worktree" }));
    expect(rememberedBrowseLocation(SESSION)).toBe("/repo/.worktrees/feature");
    expect(screen.getByTestId("repository")).toHaveTextContent(".worktrees/feature");
    expect(screen.getByTestId("changes-review-folder")).toHaveTextContent("feature");
    expect(paletteLabels()).toEqual(
      expect.arrayContaining(["Choose Folder for Diffs…", "Show repo in Diffs"]),
    );

    await userEvent.click(screen.getByTestId("changes-review-folder"));
    await userEvent.click(screen.getByRole("button", { name: "Pick workspace" }));
    expect(rememberedBrowseLocation(SESSION)).toBeNull();
    expect(screen.getByTestId("repository")).toBeEmptyDOMElement();
  });

  it("opens the folder browser from ⌘K, and returns to the workspace from it", async () => {
    render(<Harness />);
    act(() => void paletteCommand("Choose Folder for Diffs…")!.run());
    expect(screen.getByTestId("picker")).toHaveAttribute("data-initial", "/repo");
    await userEvent.click(screen.getByRole("button", { name: "Pick worktree" }));
    expect(rememberedBrowseLocation(SESSION)).toBe("/repo/.worktrees/feature");

    act(() => void paletteCommand("Show repo in Diffs")!.run());
    expect(rememberedBrowseLocation(SESSION)).toBeNull();
    expect(screen.getByTestId("repository")).toBeEmptyDOMElement();
  });

  it("names a worktree outside the workspace as one Diffs can't review", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByTestId("changes-review-folder"));
    await userEvent.click(screen.getByRole("button", { name: "Pick linked worktree" }));
    expect(screen.getByTestId("root")).toHaveTextContent("/linked/other");
    expect(screen.getByTestId("repository")).toHaveTextContent("outside");
    expect(paletteLabels()).toContain("Show repo in Diffs");
  });

  it("follows a folder Files moved to inside a worktree", () => {
    render(<Harness />);
    act(() => rememberBrowseLocation(SESSION, "/repo/.worktrees/feature/src"));
    expect(screen.getByTestId("root")).toHaveTextContent("/repo/.worktrees/feature");
    act(() => rememberBrowseLocation(SESSION, "/repo/src"));
    expect(screen.getByTestId("repository")).toBeEmptyDOMElement();
  });

  it.each([
    ["a viewer who isn't the owner", () => (mocks.permissionLevel = 1)],
    ["a confined session", () => (mocks.unconfined = false)],
  ])("shows the name plainly, with no browser or command, for %s", (_case, setup) => {
    setup();
    render(<Harness />);
    expect(screen.queryByTestId("changes-review-folder")).toBeNull();
    expect(paletteLabels()).not.toContain("Choose Folder for Diffs…");
  });

  it("looks up the worktrees for a folder it may move to, telling which one a folder reviews", () => {
    const { result, rerender } = renderHook(
      ({ lookUp }) => useReviewFolder(SESSION, false, lookUp),
      {
        initialProps: { lookUp: null as string | null },
      },
    );
    // Off screen on the workspace, nothing asks for the worktrees, so a worktree reads as the workspace.
    expect(result.current.rootFor("/repo/.worktrees/feature")).toBeNull();
    rerender({ lookUp: "/repo/.worktrees/feature" });
    expect(result.current.resolved).toBe(true);
    expect(result.current.rootFor("/repo/.worktrees/feature/src")).toBe("/repo/.worktrees/feature");
    expect(result.current.rootFor("/repo/.worktrees/gone")).toBeNull();
    expect(result.current.rootFor(null)).toBeNull();
    expect(rememberedBrowseLocation(SESSION)).toBeNull();
  });

  it("can't choose a folder in a workspace inside a larger repository, and says why", () => {
    mocks.workspace = "/monorepo/app";
    mocks.worktrees = LARGER_REPOSITORY;
    render(<Harness />);
    expect(mocks.worktreeLookups).toContain("/monorepo/app");
    expect(screen.queryByTestId("changes-review-folder")).toBeNull();
    const name = screen.getByTestId("changes-review-folder-fixed");
    expect(name).toHaveTextContent("app");
    expect(name).toHaveAttribute("aria-description", LARGER_REPOSITORY_REASON);
    expect(paletteCommand("Choose Folder for Diffs…")?.availability).toEqual({
      available: false,
      reason: LARGER_REPOSITORY_REASON,
    });

    // A folder Files moved to inside its worktree still reviews the workspace.
    act(() => rememberBrowseLocation(SESSION, "/monorepo/app/.worktrees/feature/src"));
    expect(screen.getByTestId("root")).toHaveTextContent("/monorepo/app");
    expect(screen.getByTestId("repository")).toBeEmptyDOMElement();
  });

  it.each(["pending", "unknown"] as const)(
    "offers no way to choose a folder while the worktree lookup is %s",
    (answer) => {
      // Inside a larger repository, a choice made before the lookup answers would move Files alone.
      mocks.workspace = "/monorepo/app";
      mocks.worktrees = LARGER_REPOSITORY;
      mocks.worktreeAnswer = answer;
      const { rerender } = render(<Harness />);
      expect(screen.queryByTestId("changes-review-folder")).toBeNull();
      expect(paletteCommand("Choose Folder for Diffs…")).toBeUndefined();
      expect(rememberedBrowseLocation(SESSION)).toBeNull();

      mocks.worktreeAnswer = "ok";
      rerender(<Harness />);
      expect(screen.getByTestId("changes-review-folder-fixed")).toHaveTextContent("app");
      expect(paletteCommand("Choose Folder for Diffs…")?.availability).toEqual({
        available: false,
        reason: LARGER_REPOSITORY_REASON,
      });
    },
  );

  it.each(["not_git", "denied"] as const)(
    "forgets the last list once the lookup answers %s, so a later failure doesn't bring it back",
    (answer) => {
      act(() => rememberBrowseLocation(SESSION, "/repo/.worktrees/feature"));
      const { result, rerender } = renderHook(() => useReviewFolder(SESSION, true));
      expect(result.current.repository).toBe(".worktrees/feature");

      mocks.worktreeAnswer = answer;
      rerender();
      expect(result.current.repository).toBe("");

      // The next lookup fails: the worktree that answer ruled out stays gone, and so does the answer.
      mocks.worktreeAnswer = "unknown";
      rerender();
      expect(result.current.repository).toBe("");
      expect(result.current.resolved).toBe(false);
    },
  );

  it("keeps the last list for a review that opens after the lookup failed", () => {
    rememberBrowseLocation(SESSION, "/repo/.worktrees/feature");
    const first = renderHook(() => useReviewFolder(SESSION, true));
    expect(first.result.current.root).toBe("/repo/.worktrees/feature");
    mocks.worktreeAnswer = "unknown";
    first.rerender();
    // Another Diffs tab of the session mounts while the shared lookup reads as failed.
    const second = renderHook(() => useReviewFolder(SESSION, true));
    expect(second.result.current.root).toBe("/repo/.worktrees/feature");
    expect(second.result.current.resolved).toBe(true);
    expect(second.result.current.canChoose).toBe(true);
  });

  it.each([
    ["another session", () => "conv_other"],
    ["another host", () => ((mocks.hostId = "host_2"), SESSION)],
    ["another workspace", () => ((mocks.workspace = "/other"), SESSION)],
  ])("keeps no list for a review of %s", (_case, switchTo) => {
    renderHook(() => useReviewFolder(SESSION, true));
    mocks.worktreeAnswer = "unknown";
    const session = switchTo();
    const other = renderHook(() => useReviewFolder(session, true));
    expect(other.result.current.resolved).toBe(false);
  });

  it("isn't resolved while the worktree lookup fails, then resolves on the list", () => {
    mocks.worktreeAnswer = "unknown";
    const { result, rerender } = renderHook(() =>
      useReviewFolder(SESSION, true, "/repo/.worktrees/feature"),
    );
    expect(result.current.resolved).toBe(false);
    expect(result.current.canChoose).toBe(false);
    mocks.worktreeAnswer = "ok";
    rerender();
    expect(result.current.resolved).toBe(true);
    expect(result.current.canChoose).toBe(true);
    expect(result.current.rootFor("/repo/.worktrees/feature")).toBe("/repo/.worktrees/feature");
  });

  it("is resolved on the workspace for a session that has no host", () => {
    mocks.hostId = null;
    mocks.worktreeAnswer = "pending";
    const { result } = renderHook(() => useReviewFolder(SESSION, true));
    expect(result.current.resolved).toBe(true);
    expect(result.current.root).toBe("/repo");
    expect(result.current.canChoose).toBe(false);
  });

  it("waits for a child session's host, which comes from its parent", () => {
    mocks.hostId = null;
    mocks.parentSessionId = "conv_parent";
    mocks.worktreeAnswer = "pending";
    const { result, rerender } = renderHook(() => useReviewFolder(SESSION, true));
    expect(result.current.resolved).toBe(false);
    mocks.hostId = "host_1";
    mocks.worktreeAnswer = "ok";
    rerender();
    expect(result.current.resolved).toBe(true);
  });

  it("offers no command while Diffs is off screen", () => {
    render(<Harness active={false} />);
    expect(paletteLabels()).not.toContain("Choose Folder for Diffs…");
  });

  it("keeps the shown review's commands while another Diffs tab of the session is hidden or closed", () => {
    // Two Diffs tabs of one session stay mounted; the earlier one is shown again, then the
    // hidden one closes.
    function TwoReviews({ shown, both }: { shown: "first" | "second"; both: boolean }) {
      return (
        <>
          <Harness active={shown === "first"} />
          {both ? <Harness active={shown === "second"} /> : null}
        </>
      );
    }
    const view = render(<TwoReviews shown="second" both />);
    expect(paletteLabels()).toContain("Choose Folder for Diffs…");
    view.rerender(<TwoReviews shown="first" both />);
    expect(paletteLabels()).toContain("Choose Folder for Diffs…");
    view.rerender(<TwoReviews shown="first" both={false} />);
    expect(paletteLabels()).toContain("Choose Folder for Diffs…");
    view.unmount();
    expect(paletteLabels()).not.toContain("Choose Folder for Diffs…");
  });
});
