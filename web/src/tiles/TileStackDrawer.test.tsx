import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { TileStackDrawer } from "./TileStackDrawer";

function row(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: null,
    status: "idle",
    workspace: "/workspace/example-app",
  };
}

function renderDrawer(stacked: Conversation[], quietIds = new Set<string>()) {
  const onUnstack = vi.fn();
  const onOpenSingle = vi.fn();
  render(
    <TooltipProvider>
      <TileStackDrawer
        stacked={stacked}
        quietIds={quietIds}
        onUnstack={onUnstack}
        onOpenSingle={onOpenSingle}
      />
    </TooltipProvider>,
  );
  return { onUnstack, onOpenSingle };
}

describe("TileStackDrawer", () => {
  it("explains the Stack when it is empty", () => {
    renderDrawer([]);
    expect(screen.getByText(/The Stack is empty/)).toBeInTheDocument();
  });

  it("lists parked and quiet agents with Unstack and Open", () => {
    const parked = row("a", "Parked agent");
    const quiet = row("b", "Quiet agent");
    const { onUnstack, onOpenSingle } = renderDrawer([parked, quiet], new Set(["b"]));

    expect(screen.getAllByTestId("tiles-stack-row")).toHaveLength(2);
    expect(screen.getByText("example-app · parked")).toBeInTheDocument();
    expect(screen.getByText("example-app · quiet")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: /unstack/i })[0]);
    expect(onUnstack).toHaveBeenCalledWith(parked);
    fireEvent.click(screen.getByRole("button", { name: /open quiet agent/i }));
    expect(onOpenSingle).toHaveBeenCalledWith("b");
  });
});
