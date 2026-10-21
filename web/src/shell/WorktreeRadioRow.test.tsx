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

  it("uses compact modal dimensions and type for spacious rows", () => {
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
          variant="spacious"
        />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("worktree-row")).toHaveClass(
      "h-7",
      "rounded-md",
      "px-2",
      "py-[3px]",
      "text-ui",
      "leading-4",
    );
    expect(screen.getByText("auth-refresh")).toHaveClass("font-normal", "leading-4");
    expect(screen.getByText("auth-refresh")).not.toHaveClass("font-medium");
    expect(screen.getByText("Unknown")).toHaveClass("text-xs", "leading-4");
  });

  it("offers open and copy actions for spacious rows", async () => {
    const onOpen = vi.fn();
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
          variant="spacious"
          onOpen={onOpen}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole("button", { name: "Worktree actions for auth-refresh" })).toHaveClass(
      "size-5",
    );
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Worktree actions for auth-refresh" }),
      {
        button: 0,
      },
    );
    expect(screen.queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
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
  });

  it("uses the modal's compact row contract in selectors", () => {
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
          checked
          name="worktree"
          onSelect={vi.fn()}
          testId="worktree-row"
          variant="selector"
        />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("worktree-row")).toHaveClass(
      "h-7",
      "shrink-0",
      "rounded-md",
      "px-2",
      "py-[3px]",
      "text-ui",
      "leading-4",
      "bg-muted",
    );
    expect(screen.getByTestId("worktree-row")).toHaveClass("hover:bg-muted");
    expect(screen.getByTestId("worktree-row")).not.toHaveClass(
      "hover:bg-muted/50",
      "focus-within:bg-muted",
      "focus-within:ring-1",
    );
    expect(screen.getByRole("radio")).toHaveClass("size-4", "appearance-none", "rounded-full");
    expect(screen.getByRole("radio")).not.toHaveClass("sr-only");
    expect(screen.getByText("auth-refresh")).toHaveClass("font-normal", "leading-4");
    expect(screen.getByText("Unknown")).toHaveClass("text-xs", "leading-4");
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
    expect(screen.getByTestId("worktree-row-tooltip-title")).toHaveTextContent("auth-refresh");
    expect(screen.getByTestId("worktree-row-tooltip-title")).toHaveTextContent("2h");
    expect(screen.getByTestId("worktree-row-tooltip-path")).toHaveTextContent(
      "/Users/corey/repo-worktrees/auth-refresh",
    );
    expect(screen.getByTestId("worktree-row-tooltip-branch")).toHaveTextContent(
      "feature/auth-refresh",
    );
    expect(screen.getByTestId("worktree-row-tooltip-status")).toHaveTextContent("Checked out");
    expect(tooltip).toHaveClass(
      "w-64",
      "rounded-lg",
      "bg-popover",
      "p-2.5",
      "text-popover-foreground",
      "shadow-menu",
      "ring-1",
    );
  });
});
