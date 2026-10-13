import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MenuItem } from "./menu-item";

describe("MenuItem", () => {
  it("owns the shared compact menu dimensions and interaction surface", () => {
    render(
      <MenuItem active density="compact" data-testid="item">
        Item
      </MenuItem>,
    );

    expect(screen.getByTestId("item")).toHaveClass(
      "h-7",
      "gap-2",
      "rounded-md",
      "px-2",
      "py-[3px]",
      "text-ui",
      "cursor-pointer",
      "hover:bg-muted",
      "bg-muted",
    );
    expect(screen.getByTestId("item")).not.toHaveClass("focus-within:bg-muted");
  });

  it("applies the same styling through asChild", () => {
    render(
      <MenuItem asChild density="compact">
        <button type="button">Choose</button>
      </MenuItem>,
    );

    expect(screen.getByRole("button", { name: "Choose" })).toHaveAttribute(
      "data-slot",
      "menu-item",
    );
    expect(screen.getByRole("button", { name: "Choose" })).toHaveClass(
      "h-7",
      "cursor-pointer",
      "hover:bg-muted",
    );
  });
});
