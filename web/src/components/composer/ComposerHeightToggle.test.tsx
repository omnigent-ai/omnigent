import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ComposerHeightToggle } from "./ComposerHeightToggle";

describe("ComposerHeightToggle", () => {
  it("names what it does next, and toggles without moving the caret", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<ComposerHeightToggle expanded={false} onToggle={onToggle} />);
    const toggle = screen.getByRole("button", { name: "Expand message box" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    // A mouse-down that isn't prevented would move focus, and the caret, out of the draft.
    expect(fireEvent.mouseDown(toggle)).toBe(false);
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledOnce();

    rerender(<ComposerHeightToggle expanded onToggle={onToggle} />);
    expect(screen.getByRole("button", { name: "Collapse message box" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
