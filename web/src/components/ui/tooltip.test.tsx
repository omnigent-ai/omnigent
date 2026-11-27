import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { Popover, PopoverContent, PopoverTrigger } from "./popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "./tooltip";

afterEach(cleanup);

describe("TooltipTrigger inside a popover trigger", () => {
  it("gets focus back when the popover closes with Escape", async () => {
    render(
      <TooltipProvider>
        <Popover>
          <Tooltip>
            <PopoverTrigger asChild>
              <TooltipTrigger>Checks</TooltipTrigger>
            </PopoverTrigger>
            <TooltipContent>2 failing</TooltipContent>
          </Tooltip>
          <PopoverContent aria-label="Checks list">Jobs</PopoverContent>
        </Popover>
      </TooltipProvider>,
    );
    const trigger = screen.getByRole("button", { name: "Checks" });
    await userEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Checks list" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Checks list" })).toBeNull();
    expect(trigger).toHaveFocus();
  });
});
