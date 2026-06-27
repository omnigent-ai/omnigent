import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupTextarea } from "./input-group";

describe("InputGroupAddon", () => {
  it("focuses the textarea when addon whitespace is clicked", () => {
    render(
      <InputGroup>
        <InputGroupTextarea aria-label="Message" />
        <InputGroupAddon data-testid="addon">Tools</InputGroupAddon>
      </InputGroup>,
    );

    fireEvent.click(screen.getByTestId("addon"));

    expect(screen.getByLabelText("Message")).toHaveFocus();
  });

  it("keeps focusing inputs when addon whitespace is clicked", () => {
    render(
      <InputGroup>
        <InputGroupInput aria-label="Search" />
        <InputGroupAddon data-testid="addon">Search tools</InputGroupAddon>
      </InputGroup>,
    );

    fireEvent.click(screen.getByTestId("addon"));

    expect(screen.getByLabelText("Search")).toHaveFocus();
  });

  it("does not steal focus when clicking a button inside the addon", () => {
    const onClick = vi.fn();
    render(
      <InputGroup>
        <InputGroupTextarea aria-label="Message" />
        <InputGroupAddon>
          <button type="button" onClick={onClick}>
            Attach
          </button>
        </InputGroupAddon>
      </InputGroup>,
    );
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    const focus = vi.spyOn(textarea, "focus");

    fireEvent.click(screen.getByRole("button", { name: "Attach" }));

    expect(onClick).toHaveBeenCalledOnce();
    expect(focus).not.toHaveBeenCalled();
    expect(textarea).not.toHaveFocus();
  });
});
