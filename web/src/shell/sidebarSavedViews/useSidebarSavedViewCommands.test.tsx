import { act, renderHook } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import { readSavedViews, saveView, type SidebarSavedView } from "@/lib/sidebarSavedViews";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { useSidebarSavedViewCommands } from "./useSidebarSavedViewCommands";

vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));

const SCOPE = { server: "https://example.com", userId: "user-1" };
const SETTINGS = {
  grouping: "default",
  ordering: "updated",
  show: [],
  sessionFilter: "all",
  pullRequestFilter: "open",
  statusFilter: "all",
  harnessFilter: null,
  projectFilter: { kind: "all" },
} as const;

function savedView(name: string): SidebarSavedView {
  const result = saveView(SCOPE, name, SETTINGS);
  if (!result.ok) throw new Error("not saved");
  return result.view;
}

function renderCommands(scope: typeof SCOPE | null, views: SidebarSavedView[]) {
  const apply = vi.fn();
  const openSave = vi.fn();
  renderHook(() => useSidebarSavedViewCommands({ scope, views, apply, openSave }));
  return { apply, openSave };
}

const labels = () => registeredPaletteCommands().map((command) => command.label);
const command = (label: string) => registeredPaletteCommands().find((row) => row.label === label)!;

afterEach(() => {
  localStorage.clear();
  usePaletteCommandsStore.setState({ groups: {} });
  vi.clearAllMocks();
});

describe("useSidebarSavedViewCommands", () => {
  it("offers each view, saving the sidebar, and deleting each view", () => {
    const views = [savedView("Review"), savedView("Mine")];
    renderCommands(SCOPE, views);
    expect(labels()).toEqual([
      "Sidebar view: Review",
      "Sidebar view: Mine",
      "Save sidebar view…",
      "Delete sidebar view: Review",
      "Delete sidebar view: Mine",
    ]);
    // A row built for another server or user never runs here.
    expect(command("Sidebar view: Review").id).toContain(`${SCOPE.server}:${SCOPE.userId}`);
  });

  it("offers nothing until the scope is known", () => {
    renderCommands(null, []);
    expect(labels()).toEqual([]);
  });

  it("applies a view, and opens the save dialog once the palette has closed", () => {
    const view = savedView("Review");
    const { apply, openSave } = renderCommands(SCOPE, [view]);
    act(() => command("Sidebar view: Review").run());
    expect(apply).toHaveBeenCalledWith(view);
    expect(command("Save sidebar view…").movesFocus).toBe(true);
    act(() => command("Save sidebar view…").run());
    expect(openSave).toHaveBeenCalledOnce();
  });

  it("deletes a view with an Undo that puts it back", () => {
    const view = savedView("Review");
    renderCommands(SCOPE, [view]);
    const remove = command("Delete sidebar view: Review");
    expect(remove.destructive).toBe(true);
    act(() => remove.run());
    expect(readSavedViews(SCOPE)).toEqual([]);
    expect(toast).toHaveBeenCalledWith(
      "Deleted the “Review” view",
      expect.objectContaining({ action: expect.objectContaining({ label: "Undo" }) }),
    );
    const [, options] = vi.mocked(toast).mock.calls[0]!;
    act(() => (options as unknown as { action: { onClick: () => void } }).action.onClick());
    expect(readSavedViews(SCOPE).map((saved) => saved.name)).toEqual(["Review"]);
  });

  it("runs nothing for a row accepted before the scope changed", () => {
    const view = savedView("Review");
    const apply = vi.fn();
    const openSave = vi.fn();
    const other = { ...SCOPE, userId: "user-2" };
    const { rerender } = renderHook(
      ({ scope }) => useSidebarSavedViewCommands({ scope, views: [view], apply, openSave }),
      { initialProps: { scope: SCOPE } },
    );
    // Accepted for user-1; the palette closes after user-2 has signed in.
    const queuedSave = command("Save sidebar view…").run;
    const queuedApply = command("Sidebar view: Review").run;
    const queuedDelete = command("Delete sidebar view: Review").run;
    rerender({ scope: other });
    act(() => {
      queuedSave();
      queuedApply();
      queuedDelete();
    });
    expect(openSave).not.toHaveBeenCalled();
    expect(apply).not.toHaveBeenCalled();
    expect(readSavedViews(SCOPE)).toHaveLength(1);
  });

  it("says why an Undo couldn't bring a view back", () => {
    const view = savedView("Review");
    renderCommands(SCOPE, [view]);
    act(() => command("Delete sidebar view: Review").run());
    saveView(SCOPE, "REVIEW", SETTINGS);
    const [, options] = vi.mocked(toast).mock.calls[0]!;
    act(() => (options as unknown as { action: { onClick: () => void } }).action.onClick());
    expect(toast.error).toHaveBeenCalledWith(
      "Couldn't bring back “Review”: a view with this name already exists.",
    );
    expect(readSavedViews(SCOPE).map((saved) => saved.name)).toEqual(["REVIEW"]);
  });

  it("says so when a delete can't be stored", () => {
    const view = savedView("Review");
    renderCommands(SCOPE, [view]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    act(() => command("Delete sidebar view: Review").run());
    expect(toast.error).toHaveBeenCalledWith("Couldn't delete the view. Try again.");
    vi.restoreAllMocks();
    expect(readSavedViews(SCOPE)).toHaveLength(1);
  });
});
