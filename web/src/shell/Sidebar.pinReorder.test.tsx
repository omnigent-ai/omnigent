import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// Dropping an unpinned row onto a pinned row pins it into that slot. With no
// gap between the pins every value is renumbered: the new pin goes through the
// pin toggle and, once it's accepted, the existing pins through the batch
// reorder. The toggle is a real mutation so per-call callback behaviour holds.
// Unpinning from a row offers an Undo that re-pins into the same slot.
const mocks = vi.hoisted(() => ({
  pinned: [] as ReturnType<typeof conv>[],
  filterHonored: true,
  pinFn: vi.fn(),
  reorderPins: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  const { useMutation } = await import("@tanstack/react-query");
  const { PIN_WRITE_MUTATION_KEY } = await import("@/lib/sessionListCache");
  return {
    ...conversationHooksMock(),
    useProjects: vi.fn(() => ({ data: [] })),
    usePinnedConversations: () => ({
      data: { conversations: mocks.pinned, filterHonored: mocks.filterHonored },
      isSuccess: true,
    }),
    useTogglePinnedConversation: () =>
      useMutation({
        mutationKey: PIN_WRITE_MUTATION_KEY,
        mutationFn: (vars: unknown) => mocks.pinFn(vars),
      }),
    useReorderPinnedConversations: () => ({ mutate: mocks.reorderPins }),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { Toaster } from "@/components/ui/sonner";
import { toast } from "sonner";

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

async function dropOnto(sourceId: string, targetId: string, { expectTarget = true } = {}) {
  const source = screen.getByRole("link", { name: sourceId }).closest("li")!;
  const start = rowCenter(sourceId);
  const target = rowCenter(targetId);
  fireEvent.mouseDown(source, { button: 0, ...start });
  fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
  await act(async () => {
    fireEvent.mouseMove(document, target);
  });
  expect(screen.queryByTestId("pin-order-insertion") !== null).toBe(expectTarget);
  await act(async () => {
    fireEvent.mouseUp(document, target);
  });
}

beforeEach(() => {
  mocks.filterHonored = true;
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
      expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([{ id: "conv_b", pinnedAt: 1002 }]),
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

  it("ignores a second drop while the first pin is saving, and still renumbers for the first", async () => {
    let resolveFirst!: () => void;
    mocks.pinFn.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveFirst = resolve;
        }),
    );
    renderSidebar();

    await dropOnto("conv_c", "conv_b");
    await waitFor(() => expect(mocks.pinFn).toHaveBeenCalledOnce());
    // Pinned rows take no drops while a pin write is saving.
    await dropOnto("conv_d", "conv_a", { expectTarget: false });
    expect(mocks.pinFn).toHaveBeenCalledOnce();

    await act(async () => {
      resolveFirst();
    });
    await waitFor(() =>
      expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([{ id: "conv_b", pinnedAt: 1002 }]),
    );
  });

  it("offers no pinned-row reordering when the server can't store pins", async () => {
    mocks.filterHonored = false;
    mocks.pinFn.mockResolvedValue({});
    renderSidebar();

    await dropOnto("conv_b", "conv_a", { expectTarget: false });

    expect(mocks.reorderPins).not.toHaveBeenCalled();
    expect(mocks.pinFn).not.toHaveBeenCalled();
  });

  it("disables the other rows' pin buttons while a pin is saving", async () => {
    let resolvePin!: () => void;
    mocks.pinFn.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolvePin = resolve;
        }),
    );
    renderSidebar();
    const pinButton = (id: string) =>
      screen
        .getByRole("link", { name: id })
        .closest("li")!
        .querySelector<HTMLElement>('[data-testid="quick-pin-conversation"]')!;

    fireEvent.click(pinButton("conv_c"));
    await waitFor(() => expect(mocks.pinFn).toHaveBeenCalledOnce());
    await waitFor(() => expect(pinButton("conv_d")).toHaveAttribute("aria-disabled", "true"));
    fireEvent.click(pinButton("conv_d"));
    expect(mocks.pinFn).toHaveBeenCalledOnce();

    await act(async () => {
      resolvePin();
    });
    await waitFor(() => expect(pinButton("conv_d")).toHaveAttribute("aria-disabled", "false"));
  });
});

describe("unpinning from a sidebar row", () => {
  const row = (id: string) => screen.getByRole("link", { name: id }).closest("li")!;

  // Sonner keeps toasts in module state across mounts.
  afterEach(() => {
    toast.dismiss();
  });

  it.each([
    [
      "quick-pin button",
      () => fireEvent.click(row("conv_a").querySelector('[data-testid="quick-pin-conversation"]')!),
    ],
    [
      "kebab Unpin item",
      () => {
        // Radix DropdownMenu opens on pointerdown, not click.
        fireEvent.pointerDown(
          row("conv_a").querySelector('[data-testid="conversation-actions"]')!,
          {
            button: 0,
          },
        );
        fireEvent.click(screen.getByTestId("pin-conversation"));
      },
    ],
  ])("offers an Undo from the %s that re-pins into the old slot", async (_name, unpin) => {
    mocks.pinFn.mockResolvedValue({});
    render(<Toaster />);
    renderSidebar();

    unpin();

    await waitFor(() =>
      expect(mocks.pinFn).toHaveBeenCalledExactlyOnceWith({ id: "conv_a", pinned: false }),
    );
    const pill = await screen.findByTestId("unpin-undo-toast-item");
    expect(pill).toHaveTextContent("Unpinned session");
    expect(pill).toHaveTextContent("conv_a");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(mocks.pinFn).toHaveBeenLastCalledWith({ id: "conv_a", pinned: true, pinnedAt: 1000 }),
    );
  });
});
