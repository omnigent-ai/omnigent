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
}));
vi.mock("./FileViewerContext", () => ({ useWorkspacePaths: () => ({ root: mocks.workspace }) }));
vi.mock("./fileCommands/target", () => ({ useSessionHostId: () => "host_1" }));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { permissionLevel: mocks.permissionLevel } }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: () => ({
    data: { reachable: { unconfined: mocks.unconfined, roots: [] } },
  }),
}));
vi.mock("@/hooks/useSessionWorktrees", () => ({
  useSessionWorktrees: (_session: string, _host: string, workspace: string | null) => {
    mocks.worktreeLookups.push(workspace);
    return { data: workspace ? { status: "ok", worktrees: mocks.worktrees } : undefined };
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

import { LARGER_REPOSITORY_REASON, ReviewFolderName, useReviewFolder } from "./ChangesReviewFolder";

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
});
afterEach(() => rememberBrowseLocation(SESSION, null));

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

  it("offers no command while Diffs is off screen", () => {
    render(<Harness active={false} />);
    expect(paletteLabels()).not.toContain("Choose Folder for Diffs…");
  });
});
