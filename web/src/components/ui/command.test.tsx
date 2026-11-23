import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Command, CommandInput, CommandItem, CommandList } from "./command";

afterEach(cleanup);

describe("Command Ctrl-N / Ctrl-P", () => {
  it("moves the selection like the arrows, and marks its input as a pick list's", () => {
    render(
      <Command label="Labels">
        <CommandInput placeholder="Filter labels" />
        <CommandList>
          <CommandItem>bug</CommandItem>
          <CommandItem disabled>wontfix</CommandItem>
          <CommandItem>feature</CommandItem>
        </CommandList>
      </Command>,
    );
    const input = screen.getByPlaceholderText("Filter labels");
    const selected = () => screen.getByRole("option", { selected: true });
    expect(input).toHaveAttribute("data-pick-list-input");
    expect(selected()).toHaveTextContent("bug");
    fireEvent.keyDown(input, { key: "n", code: "KeyN", ctrlKey: true });
    expect(selected()).toHaveTextContent("feature");
    fireEvent.keyDown(input, { key: "p", code: "KeyP", ctrlKey: true });
    expect(selected()).toHaveTextContent("bug");
  });
});
