import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PastedTextHintRow } from "./PastedTextHintRow";

const file = new File(["text"], "pasted-text-1.txt", { type: "text/plain" });

describe("PastedTextHintRow", () => {
  it("says the paste's size and restores it, showing ⌘Z only while that undoes it", () => {
    const onInsertAsText = vi.fn();
    const { rerender } = render(
      <PastedTextHintRow
        hint={{ file, lines: 1200, undoable: true }}
        onInsertAsText={onInsertAsText}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Large paste (1,200 lines) added as an attachment.",
    );
    expect(screen.getByText("Z")).toBeInTheDocument();

    const button = screen.getByRole("button", { name: "Insert as text" });
    // Pressing it keeps focus in the message.
    expect(fireEvent.mouseDown(button)).toBe(false);
    fireEvent.click(button);
    expect(onInsertAsText).toHaveBeenCalledOnce();

    rerender(
      <PastedTextHintRow
        hint={{ file, lines: 1, undoable: false }}
        onInsertAsText={onInsertAsText}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Large paste (1 line)");
    expect(screen.queryByText("Z")).toBeNull();
  });

  it("disables the restore while the draft is locked", () => {
    render(
      <PastedTextHintRow
        hint={{ file, lines: 60, undoable: true }}
        disabled
        onInsertAsText={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Insert as text" })).toBeDisabled();
    expect(screen.queryByText("Z")).toBeNull();
  });
});
