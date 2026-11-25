import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TicketBoardRequesterFilter, useBoardRequesterFilter } from "./TicketBoardRequesterFilter";

function Header({ projectId }: { projectId: string }) {
  const [mine, setMine] = useBoardRequesterFilter(projectId);
  return <TicketBoardRequesterFilter mine={mine} onChange={setMine} compact />;
}

function Board({ projectId }: { projectId: string }) {
  const [mine] = useBoardRequesterFilter(projectId);
  return <p data-testid="board-mine">{mine ? "mine" : "all"}</p>;
}

describe("TicketBoardRequesterFilter", () => {
  it("keeps the header switch and the board in step, and remembers the choice", () => {
    window.localStorage.clear();
    render(
      <>
        <Header projectId="p1" />
        <Board projectId="p1" />
      </>,
    );
    const yours = screen.getByRole("button", { name: "Your tickets" });
    expect(screen.getByRole("button", { name: "All tickets" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    act(() => fireEvent.click(yours));
    expect(yours).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("board-mine")).toHaveTextContent("mine");
    expect(window.localStorage.getItem("omnigent.ticketBoard.mine.p1")).toBe("1");
  });

  it("ignores another project's change", () => {
    window.localStorage.clear();
    render(
      <>
        <Header projectId="p1" />
        <Board projectId="p2" />
      </>,
    );
    act(() => fireEvent.click(screen.getByRole("button", { name: "Your tickets" })));
    expect(screen.getByTestId("board-mine")).toHaveTextContent("all");
  });

  it("shows Needs you (N) only while something waits on the viewer, and selects it alone", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <TicketBoardRequesterFilter
        mine={false}
        onChange={() => {}}
        needsYou={{ count: 0, active: false, onSelect }}
        compact
      />,
    );
    expect(screen.queryByTestId("ticket-needs-you-filter")).toBeNull();
    rerender(
      <TicketBoardRequesterFilter
        mine
        onChange={() => {}}
        needsYou={{ count: 2, active: true, onSelect }}
        compact
      />,
    );
    const chip = screen.getByTestId("ticket-needs-you-filter");
    expect(chip).toHaveTextContent("Needs you (2)");
    expect(chip).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Your tickets" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    fireEvent.click(chip);
    expect(onSelect).toHaveBeenCalledOnce();
  });
});
