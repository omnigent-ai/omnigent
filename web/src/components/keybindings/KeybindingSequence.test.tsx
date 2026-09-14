import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseKeybinding } from "@/actions";
import { KeybindingSequence } from "./KeybindingSequence";

describe("KeybindingSequence", () => {
  it("renders one complete shortcut in one pill", () => {
    render(<KeybindingSequence sequence={parseKeybinding("primary+alt+arrowup")} />);
    const binding = screen.getByRole("img", {
      name: "Keybinding Control or Command + Alt + Up arrow",
    });
    expect(within(binding).getAllByText("Ctrl+Alt+↑")).toHaveLength(1);
    expect(binding.querySelectorAll("kbd")).toHaveLength(1);
  });
});
