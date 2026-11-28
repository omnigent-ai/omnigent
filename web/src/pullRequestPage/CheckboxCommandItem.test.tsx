import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { Command, CommandList } from "@/components/ui/command";

import { CheckboxCommandItem } from "./CheckboxCommandItem";

afterEach(cleanup);

it("says whether each choice is checked", () => {
  render(
    <Command>
      <CommandList>
        <CheckboxCommandItem checked value="bug">
          bug
        </CheckboxCommandItem>
        <CheckboxCommandItem checked={false} value="docs">
          docs
        </CheckboxCommandItem>
      </CommandList>
    </Command>,
  );
  expect(screen.getByRole("option", { name: "bug" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("option", { name: "docs" })).toHaveAttribute("aria-checked", "false");
});
