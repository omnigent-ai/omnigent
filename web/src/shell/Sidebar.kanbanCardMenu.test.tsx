import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation, useMoveToProject } from "@/hooks/useConversations";
import { ConversationMenuItems } from "./Sidebar";

const conversation: Conversation = {
  id: "conv_card",
  object: "conversation",
  title: "Card session",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
};

function renderMenu(kanbanCard: boolean) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const noop = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>
          <DropdownMenu open>
            <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
            <DropdownMenuContent>
              <ConversationMenuItems
                kanbanCard={kanbanCard}
                conversation={conversation}
                isArchived={false}
                isOwner
                sharingOff={false}
                isSingleUser={false}
                canStop
                canMarkUnread
                currentProject="Payments"
                onTogglePinned={noop}
                onMarkUnread={noop}
                onMarkRead={noop}
                moveToProject={{} as ReturnType<typeof useMoveToProject>}
                setShareOpen={noop}
                setForkOpen={noop}
                canCreateAutomation
                setAutomationOpen={noop}
                setIsEditing={noop}
                setStopOpen={noop}
                setDeleteOpen={noop}
                setLeaveOpen={noop}
                setMenuOpen={noop}
                runArchive={noop}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
}

afterEach(cleanup);

describe("ConversationMenuItems on a kanban card", () => {
  it("keeps only the actions that change the board", () => {
    expect(renderMenu(true)).toEqual([
      "Fork",
      "Rename",
      "Mark as unread",
      "Move session",
      "Stop session",
      "Archive",
      "Delete",
    ]);
  });

  it("leaves the sidebar row's pin, share and automation actions in place", () => {
    expect(renderMenu(false)).toEqual(
      expect.arrayContaining(["Pin", "Share", "Create automation"]),
    );
  });
});
