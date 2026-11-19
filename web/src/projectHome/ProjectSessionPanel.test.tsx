import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ProjectSessionPanel } from "./ProjectSessionPanel";

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { title: "Plan the feature" } }),
}));
vi.mock("@/components/chat/FullSessionChat", () => ({
  FullSessionChat: ({
    sessionId,
    transcriptTopInset,
  }: {
    sessionId: string;
    transcriptTopInset: string;
  }) => (
    <output aria-label="Chat" data-inset={transcriptTopInset}>
      {sessionId}
    </output>
  ),
}));
afterEach(cleanup);

describe("Project session ticket context", () => {
  it.each([false, true])(
    "reuses the conversation with optional return navigation (ticket=%s)",
    (withTicket) => {
      const onClose = vi.fn();
      const { container } = render(
        <MemoryRouter>
          <TooltipProvider>
            <ProjectSessionPanel
              sessionId="planner"
              onClose={onClose}
              ticket={withTicket ? { id: "ticket", title: "Build the feature" } : undefined}
            />
          </TooltipProvider>
        </MemoryRouter>,
      );
      expect(screen.getByLabelText("Chat")).toHaveTextContent("planner");
      expect(screen.getByLabelText("Chat")).toHaveAttribute(
        "data-inset",
        container.querySelector("header")?.style.height,
      );
      expect(screen.getByRole("link", { name: "Open full session" })).toHaveAttribute(
        "href",
        "/c/planner",
      );
      if (withTicket) {
        expect(screen.getByRole("link", { name: "Back to ticket" })).toHaveAttribute(
          "href",
          "/?ticket=ticket",
        );
        expect(screen.getByText("Build the feature")).toBeInTheDocument();
      } else expect(screen.queryByRole("link", { name: "Back to ticket" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Close panel" }));
      expect(onClose).toHaveBeenCalledOnce();
    },
  );
});
