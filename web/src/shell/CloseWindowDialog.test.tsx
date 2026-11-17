import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CloseWindowDialog } from "./CloseWindowDialog";

describe("CloseWindowDialog", () => {
  it("focuses Close Window, so Enter confirms, and Esc cancels", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <CloseWindowDialog prompt={{ quits: false }} onConfirm={onConfirm} onCancel={onCancel} />,
    );

    expect(screen.getByRole("heading", { name: "Close this window?" })).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Close Window" });
    expect(confirm).toHaveFocus();
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(confirm, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("says closing quits when it is the last window", () => {
    render(<CloseWindowDialog prompt={{ quits: true }} onConfirm={() => {}} onCancel={() => {}} />);
    expect(screen.getByRole("heading", { name: "Quit Omnigent?" })).toBeInTheDocument();
    expect(
      screen.getByText("This is the only open window, so closing it quits Omnigent."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quit" })).toHaveFocus();
  });

  it("renders nothing until a close is requested", () => {
    render(<CloseWindowDialog prompt={null} onConfirm={() => {}} onCancel={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
