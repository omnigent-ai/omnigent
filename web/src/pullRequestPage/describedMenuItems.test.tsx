import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { DescribedMenuItem, DescribedMenuRadioItem } from "./describedMenuItems";

afterEach(cleanup);

function openMenu(content: ReactNode) {
  render(
    <DropdownMenu open>
      <DropdownMenuTrigger>Menu</DropdownMenuTrigger>
      <DropdownMenuContent>{content}</DropdownMenuContent>
    </DropdownMenu>,
  );
}

describe("described menu rows", () => {
  it("reads a row's description without adding it to the name", () => {
    openMenu(
      <DescribedMenuItem description="Only the owner can close it.">Close</DescribedMenuItem>,
    );
    expect(screen.getByRole("menuitem", { name: "Close" })).toHaveAccessibleDescription(
      "Only the owner can close it.",
    );
  });

  it("reads a radio choice's description", () => {
    openMenu(
      <DropdownMenuRadioGroup value="all">
        <DescribedMenuRadioItem value="all" description="Every pull request the session links.">
          All
        </DescribedMenuRadioItem>
      </DropdownMenuRadioGroup>,
    );
    expect(screen.getByRole("menuitemradio", { name: "All" })).toHaveAccessibleDescription(
      "Every pull request the session links.",
    );
  });

  it("leaves a row without a description unchanged", () => {
    openMenu(<DescribedMenuItem>Copy link</DescribedMenuItem>);
    expect(screen.getByRole("menuitem", { name: "Copy link" })).not.toHaveAttribute(
      "aria-describedby",
    );
  });
});
