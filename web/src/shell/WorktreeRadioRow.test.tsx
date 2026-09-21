import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { WorktreeRadioRow } from "./WorktreeRadioRow";

const copyTextMock = vi.fn();
vi.mock("@/lib/clipboard", () => ({ copyText: (text: string) => copyTextMock(text) }));

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

  it("uses the dialog body type scale for spacious timestamps", () => {
    render(
      <TooltipProvider>
        <WorktreeRadioRow
          worktree={{
            path: "/Users/corey/repo-worktrees/auth-refresh",
            branch: "feature/auth-refresh",
            is_main: false,
            detached: false,
            updated_at: null,
          }}
          checked={false}
          name="worktree"
          onSelect={vi.fn()}
          testId="worktree-row"
          spacious
        />
      </TooltipProvider>,
    );

    expect(screen.getByText("Unknown")).toHaveClass("text-base");
  });

  it("offers open, copy, and delete actions for spacious rows", async () => {
    const onOpen = vi.fn();
    const onDelete = vi.fn();
    render(
      <TooltipProvider>
        <WorktreeRadioRow
          worktree={{
            path: "/Users/corey/repo-worktrees/auth-refresh",
            branch: "feature/auth-refresh",
            is_main: false,
            detached: false,
            updated_at: null,
          }}
          checked={false}
          name="worktree"
          onSelect={vi.fn()}
          testId="worktree-row"
          spacious
          onOpen={onOpen}
          onDelete={onDelete}
        />
      </TooltipProvider>,
    );

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Worktree actions for auth-refresh" }),
      {
        button: 0,
      },
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Open folder" }));
    expect(onOpen).toHaveBeenCalledOnce();

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Worktree actions for auth-refresh" }),
      {
        button: 0,
      },
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Copy path" }));
    expect(copyTextMock).toHaveBeenCalledWith("/Users/corey/repo-worktrees/auth-refresh");

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Worktree actions for auth-refresh" }),
      {
        button: 0,
      },
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledOnce();
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
