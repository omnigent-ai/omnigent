import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation, useMoveToProject } from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { clearSessionMuteState, MUTED_LABEL_KEY } from "@/lib/sessionMute";
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

function renderMenu(
  kanbanCard: boolean,
  { muted = false, unread = false, supported = false } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  onTestFinished(() => queryClient.clear());
  const row: Conversation = { ...conversation, labels: muted ? { [MUTED_LABEL_KEY]: "1" } : {} };
  queryClient.setQueryData(["conversation-backfill", row.id], row);
  const noop = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_mute: supported }}>
        <MemoryRouter>
          <TooltipProvider>
            <DropdownMenu open>
              <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
              <DropdownMenuContent>
                <ConversationMenuItems
                  kanbanCard={kanbanCard}
                  conversation={row}
                  isArchived={false}
                  isOwner
                  sharingOff={false}
                  isSingleUser={false}
                  canStop
                  canMarkUnread={!unread}
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
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearSessionMuteState();
});

describe("ConversationMenuItems on a kanban card", () => {
  it.each([false, true].flatMap((muted) => [false, true].map((unread) => ({ muted, unread }))))(
    "offers mute controls with muted=$muted, unread=$unread without a sidebar callback",
    async ({ muted, unread }) => {
      const fetchMock = vi.fn(
        async () =>
          ({
            ok: true,
            json: async () => ({ labels: muted ? {} : { [MUTED_LABEL_KEY]: "1" } }),
          }) as Response,
      );
      vi.stubGlobal("fetch", fetchMock);
      const items = renderMenu(true, { muted, unread, supported: true });
      expect(items.includes("Mark as unread")).toBe(!muted && !unread);
      expect(items.includes("Mark as read")).toBe(unread);
      fireEvent.click(screen.getByRole("menuitem", { name: muted ? "Unmute" : "Mute" }));
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining("/v1/sessions/conv_card"),
          expect.objectContaining({
            method: "PATCH",
            body: JSON.stringify({ labels: { [MUTED_LABEL_KEY]: muted ? "" : "1" } }),
          }),
        ),
      );
    },
  );

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
