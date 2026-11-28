import { act, renderHook } from "@testing-library/react";
import { ArchiveIcon, FolderInputIcon, Trash2Icon } from "lucide-react";
import { describe, expect, it } from "vitest";

import { useHeldMenuItems } from "./useHeldMenuItems";

const archive = { id: "archive", label: "Archive", icon: ArchiveIcon, run: () => {} };
const move = { id: "move", label: "Move", icon: FolderInputIcon, run: () => {} };
const remove = { id: "delete", label: "Delete", icon: Trash2Icon, run: () => {} };
type Action = typeof archive;

function renderHeld(actions: Action[], folded: Action[]) {
  return renderHook((props) => useHeldMenuItems(props.actions, props.folded), {
    initialProps: { actions, folded },
  });
}

describe("useHeldMenuItems", () => {
  it("lists just the folded actions while closed, following each change", () => {
    const view = renderHeld([archive, move, remove], [move, remove]);
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.items).toEqual([move, remove]);
    view.rerender({ actions: [archive, move, remove], folded: [] });
    expect(view.result.current.items).toEqual([]);
  });

  it("keeps what it listed while open, though the bar widens and nothing folds", () => {
    const view = renderHeld([archive, move, remove], [move, remove]);
    act(() => view.result.current.onOpenChange(true));
    view.rerender({ actions: [archive, move, remove], folded: [] });
    expect(view.result.current.open).toBe(true);
    expect(view.result.current.items).toEqual([move, remove]);
  });

  it("adds what folds while open, in the bar's order", () => {
    const view = renderHeld([archive, move, remove], [remove]);
    act(() => view.result.current.onOpenChange(true));
    view.rerender({ actions: [archive, move, remove], folded: [archive, move, remove] });
    expect(view.result.current.items).toEqual([archive, move, remove]);
  });

  it("keeps an action that left the selection as its held copy, without its run", () => {
    const view = renderHeld([archive, move, remove], [move, remove]);
    act(() => view.result.current.onOpenChange(true));
    view.rerender({ actions: [archive, remove], folded: [remove] });
    const [first, second] = view.result.current.items;
    expect(first).toBe(remove);
    expect(second).toEqual({ id: "move", label: "Move", icon: FolderInputIcon });
    expect("run" in second!).toBe(false);
  });

  it("lets go of everything it held once closed", () => {
    const view = renderHeld([archive, move, remove], [move, remove]);
    act(() => view.result.current.onOpenChange(true));
    view.rerender({ actions: [archive, move, remove], folded: [] });
    act(() => view.result.current.onOpenChange(false));
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.items).toEqual([]);
  });
});
