import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SessionPullRequestFilter } from "@/lib/sessionListFilters";

import { SessionPullRequestFilterItems } from "./SessionPullRequestFilterItems";

function renderItems(value: SessionPullRequestFilter) {
  const onChange = vi.fn();
  render(
    <DropdownMenu open>
      <DropdownMenuTrigger>Filter</DropdownMenuTrigger>
      <DropdownMenuContent>
        <SessionPullRequestFilterItems value={value} onChange={onChange} />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
  return onChange;
}

describe("SessionPullRequestFilterItems", () => {
  it("lists each choice in order, with the current one checked", () => {
    renderItems("open");
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual([
      "All sessions",
      "With an open pull request",
      "With a draft pull request",
      "With a merged or closed pull request",
      "Without a pull request",
    ]);
    expect(
      items
        .filter((item) => item.getAttribute("aria-checked") === "true")
        .map((item) => item.textContent),
    ).toEqual(["With an open pull request"]);
  });

  it("reports the choice picked", () => {
    const onChange = renderItems("all");
    fireEvent.click(screen.getByTestId("session-pull-request-filter-none"));
    expect(onChange).toHaveBeenCalledWith("none");
  });
});
