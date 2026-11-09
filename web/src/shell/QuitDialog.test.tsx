import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { QuitDialog } from "./QuitDialog";

describe("QuitDialog", () => {
  it("says what stops, focuses Quit so Enter confirms, and Esc cancels", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <QuitDialog
        prompt={{ detail: "Sessions running on this computer will go offline." }}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    expect(screen.getByRole("heading", { name: "Quit Omnigent?" })).toBeInTheDocument();
    expect(
      screen.getByText("Sessions running on this computer will go offline."),
    ).toBeInTheDocument();
    const quit = screen.getByRole("button", { name: "Quit" });
    expect(quit).toHaveFocus();
    fireEvent.click(quit);
    expect(onConfirm).toHaveBeenCalledWith(false);

    fireEvent.keyDown(quit, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("passes Don't ask again, and starts the next prompt unchecked", () => {
    const onConfirm = vi.fn();
    const { rerender } = render(
      <QuitDialog prompt={{ detail: null }} onConfirm={onConfirm} onCancel={() => {}} />,
    );
    expect(screen.getByText("Your sessions keep running.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Don't ask again" }));
    fireEvent.click(screen.getByRole("button", { name: "Quit" }));
    expect(onConfirm).toHaveBeenLastCalledWith(true);

    rerender(<QuitDialog prompt={null} onConfirm={onConfirm} onCancel={() => {}} />);
    rerender(<QuitDialog prompt={{ detail: null }} onConfirm={onConfirm} onCancel={() => {}} />);
    expect(screen.getByRole("checkbox", { name: "Don't ask again" })).not.toBeChecked();
  });

  it("renders nothing until a quit is requested", () => {
    render(<QuitDialog prompt={null} onConfirm={() => {}} onCancel={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
