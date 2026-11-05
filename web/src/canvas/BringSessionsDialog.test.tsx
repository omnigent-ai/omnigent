import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type * as conversations from "@/hooks/useConversations";
import { BringSessionsDialog } from "./BringSessionsDialog";

const mutate = vi.fn();
vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversations>()),
  useMoveToProject: () => ({ mutate }),
}));

const session = (id: string, title: string) =>
  ({ id, title, labels: {} }) as unknown as conversations.Conversation;

describe("BringSessionsDialog", () => {
  it("moves only the chosen sessions into the project", () => {
    const onClose = vi.fn();
    render(
      <BringSessionsDialog
        open
        projectName="Alpha"
        sessions={[session("a", "Frontend work"), session("b", "Backend work")]}
        onClose={onClose}
      />,
    );
    const confirm = screen.getByRole("button", { name: "Add sessions" });
    expect(confirm).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "Backend work" }));
    fireEvent.click(confirm);

    expect(mutate.mock.calls).toEqual([[{ id: "b", project: "Alpha" }]]);
    expect(onClose).toHaveBeenCalled();
  });

  it("says so when there is nothing to bring", () => {
    render(<BringSessionsDialog open projectName="Alpha" sessions={[]} onClose={vi.fn()} />);
    expect(screen.getByText("No other sessions to add.")).toBeInTheDocument();
  });
});
