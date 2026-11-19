import type * as React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { AgentTile, type AgentTileVariant, type TileReorder } from "./AgentTile";

// A live tile's chat (the grid cell composer) has its own coverage; the stub
// records which session it shows and whether the tile is active.
vi.mock("./TileChat", () => ({
  TileChat: ({ sessionId, active }: { sessionId: string; active: boolean }) => (
    <div data-testid="tile-chat" data-session={sessionId} data-active={String(active)}>
      <p>transcript</p>
    </div>
  ),
  TileTerminalProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("./TileRenameInput", () => ({
  TileRenameInput: ({ title }: { title: string }) => (
    <input aria-label="Session name" defaultValue={title} />
  ),
}));
vi.mock("./TileDetails", () => ({
  TileDetails: ({ id }: { id: string }) => <div id={id} data-testid="tile-details" />,
}));

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_1",
    object: "conversation",
    title: "Fix authentication",
    created_at: 1,
    updated_at: 100,
    labels: {},
    permission_level: null,
    status: "idle",
    harness: "codex",
    workspace: "/workspace/project",
    git_branch: "main",
    ...overrides,
  };
}

function renderTile(
  props: Partial<{
    active: boolean;
    live: boolean;
    variant: AgentTileVariant;
    starred: boolean;
    onToggleStar: (conversation: Conversation) => void;
    onToggleFullScreen: (sessionId: string) => void;
    reorder: TileReorder;
  }> & {
    conversation?: Conversation;
  } = {},
) {
  const onActivate = vi.fn();
  const onOpenSingle = vi.fn();
  render(
    <TooltipProvider>
      <AgentTile
        conversation={props.conversation ?? conversation()}
        active={props.active ?? false}
        live={props.live ?? true}
        variant={props.variant ?? "tile"}
        onActivate={onActivate}
        onOpenSingle={onOpenSingle}
        starred={props.starred}
        onToggleStar={props.onToggleStar}
        onToggleFullScreen={props.onToggleFullScreen}
        reorder={props.reorder}
      />
    </TooltipProvider>,
  );
  return { tile: screen.getByTestId("agent-tile"), onActivate, onOpenSingle };
}

describe("AgentTile", () => {
  it("shows the harness, project and branch in the header", () => {
    renderTile();
    expect(screen.getByText("Fix authentication")).toBeInTheDocument();
    expect(screen.getByText(/codex · project/)).toBeInTheDocument();
  });

  it("gives every live tile the composer, active or not", () => {
    renderTile({ active: true });
    expect(screen.getByTestId("tile-chat")).toHaveAttribute("data-active", "true");
    expect(screen.queryByText(/View only/)).not.toBeInTheDocument();
  });

  it("activates an inactive live tile when its chat is clicked", () => {
    const { onActivate } = renderTile();
    expect(screen.getByTestId("tile-chat")).toHaveAttribute("data-active", "false");
    fireEvent.click(screen.getByText("transcript"));
    expect(onActivate).toHaveBeenCalledWith("conv_1");
  });

  it("opens single agent view from its menu without activating", async () => {
    const user = userEvent.setup();
    const { onActivate, onOpenSingle } = renderTile();
    await user.click(screen.getByRole("button", { name: "More for Fix authentication" }));
    await user.click(screen.getByRole("menuitem", { name: "Open in single agent view" }));
    expect(onOpenSingle).toHaveBeenCalledWith("conv_1");
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("asks for an answer when the agent is waiting on you", () => {
    renderTile({ conversation: conversation({ pending_elicitations_count: 1 }) });
    expect(screen.getByText(/Waiting for you/)).toBeInTheDocument();
  });

  it("renders cards and over-budget tiles without streaming", () => {
    renderTile({ variant: "card" });
    expect(screen.queryByTestId("tile-chat")).not.toBeInTheDocument();
    expect(screen.getByText("/workspace/project")).toBeInTheDocument();
  });

  it("activates from the keyboard", () => {
    const { tile, onActivate } = renderTile({ live: false });
    fireEvent.keyDown(tile, { key: "Enter" });
    expect(onActivate).toHaveBeenCalledWith("conv_1");
  });

  it("stars without activating the tile", () => {
    const onToggleStar = vi.fn();
    const { onActivate } = renderTile({ starred: true, onToggleStar });
    const star = screen.getByRole("button", { name: /unstar/i });
    expect(star).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(star);
    expect(onToggleStar).toHaveBeenCalledWith(expect.objectContaining({ id: "conv_1" }));
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("hides the star when the session can't be pinned on its own", () => {
    renderTile();
    expect(screen.queryByRole("button", { name: /star/i })).not.toBeInTheDocument();
  });

  it("opens details from the title without activating the tile", () => {
    const { onActivate } = renderTile();
    const title = screen.getByTestId("agent-tile-title");
    expect(title).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(title);
    expect(title).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("tile-details")).toBeInTheDocument();
    expect(onActivate).not.toHaveBeenCalled();

    fireEvent.click(title);
    expect(screen.queryByTestId("tile-details")).not.toBeInTheDocument();
  });

  it("moves with the arrow keys on its reorder handle", () => {
    const reorder = { onMove: vi.fn(), onMoveBy: vi.fn() };
    renderTile({ reorder });
    const handle = screen.getByTestId("tile-reorder-handle");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(reorder.onMoveBy.mock.calls).toEqual([
      ["conv_1", 1],
      ["conv_1", -1],
    ]);
  });

  it("goes full screen from its button or a title double-click", () => {
    const onToggleFullScreen = vi.fn();
    const { onActivate } = renderTile({ onToggleFullScreen });
    fireEvent.click(screen.getByRole("button", { name: "Full screen Fix authentication" }));
    fireEvent.doubleClick(screen.getByTestId("agent-tile-title"));
    expect(onToggleFullScreen).toHaveBeenCalledTimes(2);
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("renames from the header with F2 or its pencil", () => {
    renderTile();
    fireEvent.keyDown(screen.getByTestId("agent-tile-title"), { key: "F2" });
    expect(screen.getByRole("textbox", { name: "Session name" })).toHaveValue("Fix authentication");
  });

  it("keeps a sub-agent's title read-only", () => {
    renderTile({ conversation: conversation({ parent_session_id: "conv_parent" }) });
    expect(
      screen.queryByRole("button", { name: "Rename Fix authentication" }),
    ).not.toBeInTheDocument();
  });

  it("marks an agent quiet for a day as Quiet", () => {
    renderTile({ conversation: conversation({ updated_at: Date.now() / 1000 - 2 * 86_400 }) });
    expect(screen.getByText("Quiet")).toBeInTheDocument();
  });

  it("waits before opening details, so a double-click goes full screen instead", () => {
    vi.useFakeTimers();
    try {
      const onToggleFullScreen = vi.fn();
      renderTile({ onToggleFullScreen });
      const title = screen.getByTestId("agent-tile-title");
      fireEvent.click(title);
      fireEvent.doubleClick(title);
      act(() => vi.advanceTimersByTime(500));
      expect(onToggleFullScreen).toHaveBeenCalledTimes(1);
      expect(title).toHaveAttribute("aria-expanded", "false");

      fireEvent.click(title);
      act(() => vi.advanceTimersByTime(500));
      expect(title).toHaveAttribute("aria-expanded", "true");
    } finally {
      vi.useRealTimers();
    }
  });

  it("sets text size from the Aa menu", async () => {
    const user = userEvent.setup();
    renderTile();
    await user.click(screen.getByRole("button", { name: "Text size for Fix authentication" }));
    await user.click(screen.getByRole("menuitem", { name: "Larger text" }));
    expect(JSON.parse(localStorage.getItem("omnigent:tiles-text-size") ?? "{}")).toEqual({
      conv_1: 1.15,
    });
  });
});
