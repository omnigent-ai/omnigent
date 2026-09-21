import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { WorktreeRadioRow } from "./WorktreeRadioRow";

describe("WorktreeRadioRow", () => {
  it("shows only the worktree name and updated timestamp in an accessible radio row", () => {
    const twoHoursAgo = Math.floor((Date.now() - 2 * 60 * 60 * 1000) / 1000);
    render(
      <TooltipProvider delayDuration={0}>
        <WorktreeRadioRow
          worktree={{
            path: "/Users/corey/repo-worktrees/auth-refresh",
            branch: "feature/auth-refresh",
            is_main: false,
            detached: false,
            updated_at: twoHoursAgo,
          }}
          checked={false}
          name="worktree"
          onSelect={vi.fn()}
          testId="worktree-row"
        />
      </TooltipProvider>,
    );

    const row = screen.getByTestId("worktree-row");
    expect(screen.getByRole("radio", { name: "Use worktree auth-refresh" })).toBeInTheDocument();
    expect(row).toHaveTextContent("auth-refresh");
    expect(row).toHaveTextContent("2h");
    expect(row).not.toHaveTextContent("feature/auth-refresh");
    expect(row).not.toHaveTextContent("/Users/corey");
  });

  it("shows a light tooltip with full path, branch, and status on focus", async () => {
    render(
      <TooltipProvider delayDuration={0}>
        <WorktreeRadioRow
          worktree={{
            path: "/Users/corey/repo-worktrees/auth-refresh",
            branch: "feature/auth-refresh",
            is_main: false,
            detached: false,
            updated_at: Math.floor((Date.now() - 2 * 60 * 60 * 1000) / 1000),
          }}
          checked
          name="worktree"
          onSelect={vi.fn()}
          testId="worktree-row"
        />
      </TooltipProvider>,
    );

    fireEvent.focus(screen.getByRole("radio"));
    const tooltip = await screen.findByTestId("worktree-row-tooltip");
    expect(tooltip).toHaveTextContent("Path: /Users/corey/repo-worktrees/auth-refresh");
    expect(tooltip).toHaveTextContent("Branch: feature/auth-refresh");
    expect(tooltip).toHaveTextContent("Status: Checked out");
    expect(tooltip).toHaveClass("bg-popover", "text-popover-foreground", "shadow-menu", "ring-1");
  });
});
