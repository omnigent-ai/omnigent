import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Behaviour tests for the peek card's entry window: while the card is still
// fading in it is (nearly) invisible yet already covers the header toggle
// whose hover armed it, so it must stay click-through — otherwise a fast
// click aimed at the toggle lands on invisible sidebar content (the brand
// link navigates the user to "/"). The card takes the pointer over only once
// its entry animation completes.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: () => false,
  isLocalServerOrigin: (origin: string) =>
    ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(new URL(origin).hostname),
}));

import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(() => conversationPage(conversations));
}

function sidebarAt(props: { open: boolean; peek?: boolean }) {
  return <Sidebar open={props.open} peek={props.peek} onClose={vi.fn()} onOpen={vi.fn()} />;
}

function renderSidebar(props: { open: boolean; peek?: boolean }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>{sidebarAt(props)}</MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
  return {
    ...view,
    rerenderSidebar: (next: { open: boolean; peek?: boolean }) =>
      view.rerender(
        <QueryClientProvider client={qc}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter initialEntries={["/"]}>{sidebarAt(next)}</MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      ),
  };
}

function card() {
  return screen.getByRole("complementary", { name: "Conversations" });
}

function endAnimation(element: Element = card()) {
  // jsdom lacks AnimationEvent, so React 18 listens for its WebKit fallback.
  fireEvent(element, new Event("webkitAnimationEnd", { bubbles: true }));
}

beforeEach(() => {
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("peek card entry window", () => {
  it("starts click-through so the toggle underneath keeps the click", () => {
    renderSidebar({ open: false, peek: true });

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("takes the pointer over once its own entry animation completes", () => {
    renderSidebar({ open: false, peek: true });

    endAnimation();

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("takes the pointer over if the entry animation end never fires", () => {
    vi.useFakeTimers();
    renderSidebar({ open: false, peek: true });

    act(() => vi.advanceTimersByTime(200));

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("is immediately interactive when reduced motion is preferred", () => {
    const defaultMatchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...defaultMatchMedia(query),
      matches: query === "(prefers-reduced-motion: reduce)",
    }));

    renderSidebar({ open: false, peek: true });

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("ignores a child's bubbling animation end", () => {
    // Rows and badges inside the card animate too; their animationend events
    // bubble to the aside and must not cut the click-through window short.
    renderSidebar({ open: false, peek: true });

    const child = card().querySelector("div");
    expect(child).not.toBeNull();
    endAnimation(child as Element);

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("is click-through again on the next peek", () => {
    const view = renderSidebar({ open: false, peek: true });
    endAnimation();
    expect(card()).not.toHaveClass("pointer-events-none");

    view.rerenderSidebar({ open: false, peek: false });
    view.rerenderSidebar({ open: false, peek: true });

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("never blocks pointer events while docked open", () => {
    renderSidebar({ open: true });

    expect(card()).not.toHaveClass("pointer-events-none");
  });
});
