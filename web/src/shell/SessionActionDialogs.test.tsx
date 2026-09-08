import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { Conversation } from "@/hooks/useConversations";
import { DeleteSessionDialog, RenameSessionDialog } from "./SessionActionDialogs";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  Link: ({ children }: { children: ReactNode }) => <a>{children}</a>,
  useNavigate: () => navigate,
}));

const renameConversation = vi.fn();
const deleteConversation = vi.fn();
vi.mock("@/hooks/useConversations", () => ({
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useRenameConversation: () => ({ mutate: renameConversation, isPending: false }),
  useStopAndDeleteConversation: () => ({ mutate: deleteConversation, isPending: false }),
}));

const CONVERSATION: Conversation = {
  id: "session-1",
  object: "conversation",
  title: "Original title",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  labels: {},
  permission_level: 3,
};

beforeEach(() => {
  navigate.mockReset();
  renameConversation.mockReset();
  deleteConversation.mockReset();
});
afterEach(cleanup);

describe("RenameSessionDialog", () => {
  it("prefills and selects the current title, then persists a trimmed rename", () => {
    const onOpenChange = vi.fn();
    render(<RenameSessionDialog conversation={CONVERSATION} open onOpenChange={onOpenChange} />);

    const input = screen.getByRole("textbox", { name: "Session name" }) as HTMLInputElement;
    expect(input).toHaveValue("Original title");
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe("Original title".length);

    fireEvent.change(input, { target: { value: "  Renamed session  " } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));

    expect(renameConversation).toHaveBeenCalledWith({
      id: "session-1",
      title: "Renamed session",
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("dismisses without renaming", () => {
    const onOpenChange = vi.fn();
    render(<RenameSessionDialog conversation={CONVERSATION} open onOpenChange={onOpenChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(renameConversation).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("DeleteSessionDialog", () => {
  it("does not delete when dismissed", () => {
    const onOpenChange = vi.fn();
    render(<DeleteSessionDialog conversation={CONVERSATION} open onOpenChange={onOpenChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(deleteConversation).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
