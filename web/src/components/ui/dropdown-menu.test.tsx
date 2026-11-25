import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "./dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "./dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";
import { shortcutAriaKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

afterEach(cleanup);

// A modal Dialog's scroll lock watches `document` for wheel events and cancels
// the ones it cannot place inside the dialog. Menus and popovers open in a
// portal outside the dialog's content, so a list inside them could not scroll.
function renderInDialog(layer: ReactNode) {
  return render(
    <Dialog open onOpenChange={() => {}}>
      <DialogContent>
        <DialogTitle>New automation</DialogTitle>
        <p data-testid="dialog-body">Body</p>
        {layer}
      </DialogContent>
    </Dialog>,
  );
}

/** True when the browser may still scroll after the wheel event: nothing cancelled it. */
function wheelScrolls(target: HTMLElement): boolean {
  return fireEvent.wheel(target, { deltaY: 120 });
}

describe("wheel inside layers portalled out of a Dialog", () => {
  it("lets a dropdown menu and its sub-menu scroll while the dialog lock cancels the rest", () => {
    renderInDialog(
      <DropdownMenu open modal={false}>
        <DropdownMenuTrigger>Claude Code</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>Codex</DropdownMenuItem>
          <DropdownMenuSub open>
            <DropdownMenuSubTrigger>Edit</DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuItem>Opus</DropdownMenuItem>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>,
    );

    expect(wheelScrolls(screen.getByRole("menuitem", { name: "Codex" }))).toBe(true);
    expect(wheelScrolls(screen.getByRole("menuitem", { name: "Opus" }))).toBe(true);
    // The lock is live: a wheel over the dialog's own body is still cancelled here.
    expect(wheelScrolls(screen.getByTestId("dialog-body"))).toBe(false);
  });

  it("lets a popover list scroll", () => {
    renderInDialog(
      <Popover open modal={false}>
        <PopoverTrigger>Every day at 9:00 AM</PopoverTrigger>
        <PopoverContent>
          <p>Hours</p>
        </PopoverContent>
      </Popover>,
    );

    expect(wheelScrolls(screen.getByText("Hours"))).toBe(true);
    expect(wheelScrolls(screen.getByTestId("dialog-body"))).toBe(false);
  });
});

describe("dropdown menu row shortcuts and descriptions", () => {
  function openMenu(content: ReactNode) {
    render(
      <DropdownMenu open>
        <DropdownMenuTrigger>Menu</DropdownMenuTrigger>
        <DropdownMenuContent>{content}</DropdownMenuContent>
      </DropdownMenu>,
    );
  }

  it("ends a bound row with its chord and reads a description without adding it to the name", () => {
    openMenu(
      <DropdownMenuItem shortcut="session.rename" description="Only the owner can rename it.">
        Rename
      </DropdownMenuItem>,
    );
    const row = screen.getByRole("menuitem", { name: /^Rename/ });
    expect(row).toHaveAttribute("aria-keyshortcuts", shortcutAriaKeys("session.rename"));
    expect(row).toHaveAccessibleDescription("Only the owner can rename it.");
  });

  it("reads a radio choice's description", () => {
    openMenu(
      <DropdownMenuRadioGroup value="all">
        <DropdownMenuRadioItem value="all" description="Every pull request the session links.">
          All
        </DropdownMenuRadioItem>
      </DropdownMenuRadioGroup>,
    );
    expect(screen.getByRole("menuitemradio", { name: "All" })).toHaveAccessibleDescription(
      "Every pull request the session links.",
    );
  });

  it("leaves a row without a binding or description unchanged", () => {
    openMenu(<DropdownMenuItem>Copy link</DropdownMenuItem>);
    const row = screen.getByRole("menuitem", { name: "Copy link" });
    expect(row).not.toHaveAttribute("aria-keyshortcuts");
    expect(row).not.toHaveAttribute("aria-describedby");
  });
});
