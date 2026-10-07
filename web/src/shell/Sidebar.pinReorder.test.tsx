import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// Dropping an unpinned row onto a pinned row pins it into that slot. With no
// gap between the pins every value is renumbered: the new pin goes through the
// pin toggle and the existing pins through the batch reorder.
const mocks = vi.hoisted(() => ({
  pinned: [] as ReturnType<typeof conv>[],
  pinAt: vi.fn(),
  reorderPins: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useProjects: vi.fn(() => ({ data: [] })),
    usePinnedConversations: () => ({
      data: { conversations: mocks.pinned, filterHonored: true },
      isSuccess: true,
    }),
    useTogglePinnedConversation: () => ({ mutate: mocks.pinAt }),
    useReorderPinnedConversations: () => ({ mutate: mocks.reorderPins }),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";

const ROW_HEIGHT = 30;

// jsdom has no layout, so give each session row a stacked rect for dnd-kit's
// pointer collision; everything else stays zero-sized and never collides.
function stubRowRects() {
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
    this: Element,
  ) {
    const rows = [...document.querySelectorAll("li[data-sidebar-session-id]")];
    const index = rows.indexOf(this);
    if (index < 0) return new DOMRect(0, 0, 0, 0);
    return new DOMRect(0, index * ROW_HEIGHT, 200, ROW_HEIGHT);
  });
}

function rowCenter(id: string) {
  const rows = [...document.querySelectorAll("li[data-sidebar-session-id]")];
  const index = rows.findIndex((row) => row.getAttribute("data-sidebar-session-id") === id);
  return { clientX: 50, clientY: index * ROW_HEIGHT + ROW_HEIGHT / 2 };
}

beforeEach(() => {
  mocks.pinned = [
    conv("conv_a", { labels: { "omnigent.pinned": "1000" } }),
    conv("conv_b", { labels: { "omnigent.pinned": "1000" } }),
  ];
  vi.mocked(useConversations).mockImplementation(() => conversationPage([conv("conv_c")]));
});

afterEach(async () => {
  fireEvent.mouseUp(document);
  cleanup();
  await new Promise((resolve) => {
    setTimeout(resolve, 50);
  });
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("dropping an unpinned session onto a pinned row", () => {
  it("pins it into that slot and renumbers the existing pins", async () => {
    mocks.pinAt.mockImplementation((_vars, options?: { onSuccess?: () => void }) =>
      options?.onSuccess?.(),
    );
    stubRowRects();
    renderSidebar();
    const source = screen.getByRole("link", { name: "conv_c" }).closest("li")!;
    const start = rowCenter("conv_c");
    const target = rowCenter("conv_b");

    fireEvent.mouseDown(source, { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
    await act(async () => {
      fireEvent.mouseMove(document, target);
    });
    expect(screen.getByTestId("pin-order-insertion")).toBeInTheDocument();
    await act(async () => {
      fireEvent.mouseUp(document, target);
    });

    expect(mocks.pinAt).toHaveBeenCalledExactlyOnceWith(
      { id: "conv_c", pinned: true, pinnedAt: 1001 },
      expect.anything(),
    );
    expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([
      { id: "conv_a", pinnedAt: 1000 },
      { id: "conv_b", pinnedAt: 1002 },
    ]);
  });

  it("leaves the existing pins alone when the new pin is rejected (e.g. at the pin cap)", async () => {
    // A rejected pin never calls the mutate-level onSuccess.
    mocks.pinAt.mockImplementation(() => undefined);
    stubRowRects();
    renderSidebar();
    const source = screen.getByRole("link", { name: "conv_c" }).closest("li")!;
    const start = rowCenter("conv_c");
    const target = rowCenter("conv_b");

    fireEvent.mouseDown(source, { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
    await act(async () => {
      fireEvent.mouseMove(document, target);
    });
    await act(async () => {
      fireEvent.mouseUp(document, target);
    });

    expect(mocks.pinAt).toHaveBeenCalledOnce();
    expect(mocks.reorderPins).not.toHaveBeenCalled();
  });
});
