import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { shortcutAriaKeys, shortcutKeys } from "@/lib/shortcuts/registry";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "./context-menu";
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

describe("context menu row shortcuts and descriptions", () => {
  function openContextMenu(item: React.ReactNode) {
    render(
      <ContextMenu>
        <ContextMenuTrigger>Row</ContextMenuTrigger>
        <ContextMenuContent>{item}</ContextMenuContent>
      </ContextMenu>,
    );
    fireEvent.contextMenu(screen.getByText("Row"));
  }

  it("shows a bound command's registry chord at the end of the row", () => {
    openContextMenu(<ContextMenuItem shortcut="session.rename">Rename</ContextMenuItem>);
    const row = screen.getByRole("menuitem", { name: /Rename/ });
    const keys = row.querySelector<HTMLElement>('[data-slot="shortcut-keys"]')!;
    expect([...keys.querySelectorAll("kbd")].map((kbd) => kbd.textContent)).toEqual(
      shortcutKeys("session.rename"),
    );
    expect(keys).toHaveClass("ml-auto", "pointer-coarse:hidden");
    expect(row).toHaveAttribute("aria-keyshortcuts", shortcutAriaKeys("session.rename"));
  });

  it("reads a description under the label without adding it to the row's name", () => {
    openContextMenu(
      <ContextMenuItem disabled description="Press ⌘V to paste here.">
        Paste
      </ContextMenuItem>,
    );
    const row = screen.getByRole("menuitem", { name: "Paste" });
    expect(row).toHaveAccessibleDescription("Press ⌘V to paste here.");
  });

  it("keeps the caller's own description and keys beside the row's", () => {
    openContextMenu(
      <>
        <span id="row-hint">Runs on the open file.</span>
        <ContextMenuItem
          shortcut="session.rename"
          description="Press ⌘V to paste here."
          aria-describedby="row-hint"
          aria-keyshortcuts="F2"
        >
          Rename
        </ContextMenuItem>
      </>,
    );
    const row = screen.getByRole("menuitem", { name: /Rename/ });
    expect(row).toHaveAccessibleDescription("Runs on the open file. Press ⌘V to paste here.");
    expect(row).toHaveAttribute("aria-keyshortcuts", "F2");
  });

  it("leaves a row without a binding or description unchanged", () => {
    openContextMenu(<ContextMenuItem>Plain</ContextMenuItem>);
    const row = screen.getByRole("menuitem", { name: "Plain" });
    expect(row.querySelector("kbd")).toBeNull();
    expect(row).not.toHaveAttribute("aria-keyshortcuts");
    expect(row).not.toHaveAttribute("aria-describedby");
  });
});
