import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SidebarSavedView } from "@/lib/sidebarSavedViews";

import { SidebarSavedViewItems } from "./SidebarSavedViewItems";

const view = (id: string, name: string): SidebarSavedView => ({
  id,
  name,
  grouping: "default",
  ordering: "updated",
  show: [],
  sessionFilter: "all",
  pullRequestFilter: "all",
  statusFilter: "all",
  harnessFilter: null,
  projectFilter: { kind: "all" },
});
const VIEWS = [view("a", "Review"), view("b", "Mine")];

function renderItems(views: SidebarSavedView[], matched: SidebarSavedView | null) {
  const handlers = { onApply: vi.fn(), onSave: vi.fn(), onDelete: vi.fn() };
  render(
    <DropdownMenu open>
      <DropdownMenuTrigger>Views</DropdownMenuTrigger>
      <DropdownMenuContent>
        <SidebarSavedViewItems views={views} matched={matched} {...handlers} />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
  return handlers;
}

describe("SidebarSavedViewItems", () => {
  it("lists the views with the one shown checked, and applies one picked", () => {
    const { onApply } = renderItems(VIEWS, VIEWS[1]!);
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual(["Review", "Mine"]);
    expect(items[1]).toHaveAttribute("aria-checked", "true");
    fireEvent.click(items[0]!);
    expect(onApply).toHaveBeenCalledWith(VIEWS[0]);
  });

  it("saves the sidebar as it is, and deletes only the view it matches", () => {
    const { onSave, onDelete } = renderItems(VIEWS, VIEWS[0]!);
    fireEvent.click(screen.getByRole("menuitem", { name: "Save current view…" }));
    expect(onSave).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete “Review”" }));
    expect(onDelete).toHaveBeenCalledWith(VIEWS[0]);
  });

  it("offers only saving with no views", () => {
    renderItems([], null);
    expect(screen.queryAllByRole("menuitemradio")).toHaveLength(0);
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Save current view…",
    ]);
  });

  it("checks no view and offers no delete when views exist but none matches", () => {
    renderItems(VIEWS, null);
    const radios = screen.getAllByRole("menuitemradio");
    expect(radios).toHaveLength(2);
    for (const radio of radios) expect(radio).toHaveAttribute("aria-checked", "false");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Save current view…",
    ]);
  });
});
