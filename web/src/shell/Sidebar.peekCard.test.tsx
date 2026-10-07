import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// The desktop peek card floats below the chat header so it never covers the
// "Open sidebar" toggle whose hover armed it: once the card is visible, a click
// on the toggle's spot must still reach the toggle, not the card's brand link.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function renderPeekingSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open={false} peek onClose={vi.fn()} />
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockImplementation(() => conversationPage([conv("conv_a")]));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("desktop peek card", () => {
  it("floats below the chat header so the toggle that armed it stays clickable", () => {
    renderPeekingSidebar();

    const card = screen.getByRole("complementary", { name: "Conversations" });
    expect(card).toHaveClass("is-peek", "md:top-12");
    expect(card).not.toHaveClass("md:inset-2");
  });
});
