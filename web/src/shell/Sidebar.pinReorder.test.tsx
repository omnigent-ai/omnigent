import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// Dropping an unpinned row onto a pinned row pins it into that slot. With no
// gap between the pins every value is renumbered: the new pin goes through the
// pin toggle and, once it's accepted, the existing pins through the batch
// reorder. The toggle is a real mutation so per-call callback behaviour holds.
const mocks = vi.hoisted(() => ({
  pinned: [] as ReturnType<typeof conv>[],
  pinFn: vi.fn(),
  reorderPins: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  const { useMutation } = await import("@tanstack/react-query");
  return {
    ...conversationHooksMock(),
    useProjects: vi.fn(() => ({ data: [] })),
    usePinnedConversations: () => ({
      data: { conversations: mocks.pinned, filterHonored: true },
      isSuccess: true,
    }),
    useTogglePinnedConversation: () =>
      useMutation({ mutationFn: (vars: unknown) => mocks.pinFn(vars) }),
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

async function dropOnto(sourceId: string, targetId: string) {
  const source = screen.getByRole("link", { name: sourceId }).closest("li")!;
  const start = rowCenter(sourceId);
  const target = rowCenter(targetId);
  fireEvent.mouseDown(source, { button: 0, ...start });
  fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
  await act(async () => {
    fireEvent.mouseMove(document, target);
  });
  expect(screen.getByTestId("pin-order-insertion")).toBeInTheDocument();
  await act(async () => {
    fireEvent.mouseUp(document, target);
  });
}

beforeEach(() => {
  mocks.pinned = [
    conv("conv_a", { labels: { "omnigent.pinned": "1000" } }),
    conv("conv_b", { labels: { "omnigent.pinned": "1000" } }),
  ];
  vi.mocked(useConversations).mockImplementation(() =>
    conversationPage([conv("conv_c"), conv("conv_d")]),
  );
  stubRowRects();
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
    mocks.pinFn.mockResolvedValue({});
    renderSidebar();

    await dropOnto("conv_c", "conv_b");

    expect(mocks.pinFn).toHaveBeenCalledExactlyOnceWith({
      id: "conv_c",
      pinned: true,
      pinnedAt: 1001,
    });
    await waitFor(() =>
      expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([
        { id: "conv_a", pinnedAt: 1000 },
        { id: "conv_b", pinnedAt: 1002 },
      ]),
    );
  });

  it("leaves the existing pins alone when the new pin is rejected (e.g. at the pin cap)", async () => {
    mocks.pinFn.mockRejectedValue(new Error("You can pin up to 30 sessions."));
    renderSidebar();

    await dropOnto("conv_c", "conv_b");

    await waitFor(() => expect(mocks.pinFn).toHaveBeenCalledOnce());
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(mocks.reorderPins).not.toHaveBeenCalled();
  });

  it("preserves the first insertion's renumber writes when a second pin starts before it settles", async () => {
    const resolvers: (() => void)[] = [];
    mocks.pinFn.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    renderSidebar();

    await dropOnto("conv_c", "conv_b");
    await dropOnto("conv_d", "conv_a");
    expect(mocks.pinFn).toHaveBeenCalledTimes(2);

    await act(async () => {
      for (const resolve of resolvers) resolve();
    });

    await waitFor(() =>
      expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([
        { id: "conv_a", pinnedAt: 1000 },
        { id: "conv_b", pinnedAt: 1002 },
      ]),
    );
  });
});
