import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { AgentTile, type AgentTileVariant } from "./AgentTile";

// The chat pane has its own suite; the stub records how the tile configures it.
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({ childId, readOnly }: { childId: string; readOnly?: boolean }) => (
    <div data-testid="side-chat" data-child={childId} data-read-only={String(Boolean(readOnly))}>
      <p>transcript</p>
    </div>
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

  it("gives only the active tile a composer", () => {
    renderTile({ active: true });
    expect(screen.getByTestId("side-chat")).toHaveAttribute("data-read-only", "false");
    expect(screen.queryByText(/View only/)).not.toBeInTheDocument();
  });

  it("keeps an inactive live tile read-only until it is clicked", () => {
    const { onActivate } = renderTile();
    expect(screen.getByTestId("side-chat")).toHaveAttribute("data-read-only", "true");
    fireEvent.click(screen.getByText("transcript"));
    expect(onActivate).toHaveBeenCalledWith("conv_1");
  });

  it("does not activate from its header controls", () => {
    const { onActivate, onOpenSingle } = renderTile();
    fireEvent.click(screen.getByRole("button", { name: /single agent view/i }));
    expect(onOpenSingle).toHaveBeenCalledWith("conv_1");
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("asks for an answer when the agent is waiting on you", () => {
    renderTile({ conversation: conversation({ pending_elicitations_count: 1 }) });
    expect(screen.getByText(/Waiting for you/)).toBeInTheDocument();
  });

  it("renders cards and over-budget tiles without streaming", () => {
    renderTile({ variant: "card" });
    expect(screen.queryByTestId("side-chat")).not.toBeInTheDocument();
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
});
