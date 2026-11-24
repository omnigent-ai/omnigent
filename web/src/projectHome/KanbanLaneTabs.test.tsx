import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { StatusBucket } from "@/shell/sidebarNav";
import { KanbanLaneTabs } from "./ProjectKanbanBoard";
import { KANBAN_COLUMNS } from "./projectKanban";

afterEach(cleanup);

function renderTabs(counts: Partial<Record<StatusBucket, number>>) {
  render(
    <KanbanLaneTabs
      cardCounts={new Map(KANBAN_COLUMNS.map((column) => [column, counts[column] ?? 0]))}
      renderLane={(column) => <p data-testid="lane">{column}</p>}
    />,
  );
}

describe("KanbanLaneTabs", () => {
  it("opens on the first lane with cards and shows one lane at a time", () => {
    renderTabs({ Working: 2, Done: 5 });
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Draft0",
      "Needs attention0",
      "Working2",
      "Unread0",
      "Done5",
    ]);
    expect(screen.getByRole("tab", { selected: true })).toHaveTextContent("Working");
    expect(screen.getByTestId("lane")).toHaveTextContent("Working");

    fireEvent.mouseDown(screen.getByRole("tab", { name: /Done/ }));
    expect(screen.getByTestId("lane")).toHaveTextContent("Done");
    expect(screen.getAllByTestId("lane")).toHaveLength(1);
  });

  it("opens on the first lane when the board is empty", () => {
    renderTabs({});
    expect(screen.getByRole("tab", { selected: true })).toHaveTextContent("Draft");
  });
});
