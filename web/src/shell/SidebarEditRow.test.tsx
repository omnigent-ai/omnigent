import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SidebarEditRow } from "./SidebarEditRow";

afterEach(cleanup);

function renderEditRow() {
  const onCommit = vi.fn();
  const onCancel = vi.fn();
  render(<SidebarEditRow initialTitle="Draft" onCommit={onCommit} onCancel={onCancel} />);
  const input = screen.getByTestId("rename-conversation-input");
  fireEvent.change(input, { target: { value: "Final" } });
  return { input, onCommit, onCancel };
}

describe("SidebarEditRow", () => {
  it("saves once when a blur follows Enter", () => {
    const { input, onCommit } = renderEditRow();

    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith("Final");
  });

  it("doesn't save on the blur that follows Escape", () => {
    const { input, onCommit, onCancel } = renderEditRow();

    fireEvent.keyDown(input, { key: "Escape" });
    fireEvent.blur(input);

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCommit).not.toHaveBeenCalled();
  });
});
