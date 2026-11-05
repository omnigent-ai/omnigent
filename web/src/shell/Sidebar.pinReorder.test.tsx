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

async function dropOnto(
  sourceId: string,
  targetId: string,
  { expectTarget = true, whileOver = () => {} } = {},
) {
  const source = screen.getAllByRole("link", { name: sourceId })[0].closest("li")!;
  const start = rowCenter(sourceId);
  const target = rowCenter(targetId);
  fireEvent.mouseDown(source, { button: 0, ...start });
  fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
  await act(async () => {
    fireEvent.mouseMove(document, target);
  });
  expect(screen.queryByTestId("pin-order-insertion") !== null).toBe(expectTarget);
  whileOver();
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
    const family = { id: "family", representative_id: "conv_b", pinned: true, pinned_at: "1000" };
    mocks.pinned[1].family = family;
    mocks.pinned.push(conv("conv_peer", { family }));
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
    await dropOnto("conv_a", "conv_peer", { expectTarget: false });
    await dropOnto("conv_peer", "conv_a", { expectTarget: false });
    expect(mocks.pinFn).toHaveBeenCalledOnce();
    expect(mocks.reorderPins).toHaveBeenCalledOnce();
  });

  it.each([false, true])(
    "reorders an individually pinned sibling (family pinned: %s)",
    async (pinned) => {
      mocks.pinned[0].family = {
        id: "family",
        representative_id: "parent",
        pinned,
        pinned_at: pinned ? "500" : null,
      };
      renderSidebar();

      await dropOnto("conv_a", "conv_b");

      expect(mocks.reorderPins).toHaveBeenCalledExactlyOnceWith([{ id: "conv_a", pinnedAt: 1001 }]);
      expect(mocks.pinFn).not.toHaveBeenCalled();
    },
  );

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

  it("unpins a sibling's individual pin while its family stays pinned", async () => {
    const family = { id: "conv_b", representative_id: "conv_b", pinned: true, pinned_at: "2000" };
    mocks.pinned = mocks.pinned.map((session) => ({ ...session, family }));
    mocks.pinFn.mockResolvedValue({});
    renderSidebar();

    fireEvent.contextMenu(screen.getByRole("link", { name: "conv_a" }));
    const unpin = screen.getByTestId("pin-conversation");
    expect(unpin).toHaveTextContent("Unpin");
    fireEvent.click(unpin);
    await waitFor(() =>
      expect(mocks.pinFn).toHaveBeenCalledExactlyOnceWith({ id: "conv_a", pinned: false }),
    );
  });

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
    await waitFor(() => expect(pill).not.toBeInTheDocument());
  });
});

describe("Custom session order", () => {
  const rowOrder = () =>
    [...document.querySelectorAll("li[data-sidebar-session-id]")].map((row) =>
      row.getAttribute("data-sidebar-session-id"),
    );
  const saved = () => JSON.parse(localStorage.getItem("omnigent:sidebar-view") ?? "{}");
  const link = (id: string) => screen.getByRole("link", { name: id });
  const renderGrouping = () => renderSidebar({ features: { sidebar_grouping_ui: true } });
  const listRows = (...extra: ReturnType<typeof conv>[]) =>
    vi
      .mocked(useConversations)
      .mockImplementation(() =>
        conversationPage([
          conv("conv_c", { created_at: 3, updated_at: 10 }),
          conv("conv_d", { created_at: 2, updated_at: 30, status: "running" }),
          conv("conv_e", { created_at: 1, updated_at: 20 }),
          ...extra,
        ]),
      );

  beforeEach(() => {
    mocks.pinned = [];
    localStorage.setItem("omnigent:sidebar-view", JSON.stringify({ ordering: "created" }));
    listRows();
  });
  afterEach(() => {
    localStorage.clear();
  });
  // The pinned-style line marks the slot: above the target when dragging up, below when down.
  const expectLine = (id: string, edge: "top" | "bottom") => {
    const line = screen.getByTestId("session-order-insertion");
    expect(link(id).closest("li")).toContainElement(line);
    expect(line).toHaveAttribute("style", `${edge}: 0px;`);
  };

  it.each([false, true])(
    "moves whole families by drag and menu (collapsed: %s)",
    async (collapsed) => {
      const family = { id: "conv_c", representative_id: "conv_c", pinned: false };
      vi.mocked(useConversations).mockImplementation(() =>
        conversationPage([
          conv("conv_c", { created_at: 1, updated_at: 10, family }),
          conv("conv_peer", { created_at: 2, updated_at: 20, family }),
          conv("conv_d", { created_at: 3, updated_at: 30 }),
        ]),
      );
      localStorage.setItem(
        "omnigent:sidebar-view",
        JSON.stringify({
          ordering: "custom",
          custom: { base: "created", since: 100, ids: ["conv_c", "conv_peer", "conv_d"] },
        }),
      );
      renderGrouping();
      if (collapsed)
        fireEvent.click(screen.getByRole("button", { name: "Collapse session family" }));
      const familyRows = collapsed ? ["conv_c"] : ["conv_c", "conv_peer"];

      await dropOnto("conv_c", "conv_d", { expectTarget: false });
      expect(rowOrder()).toEqual(["conv_d", ...familyRows]);
      // dnd-kit retains its document click guard for 50 ms after a drag.
      await act(async () => {
        await new Promise((resolve) => {
          setTimeout(resolve, 50);
        });
      });
      fireEvent.pointerDown(
        link("conv_c").closest("li")!.querySelector('[data-testid="conversation-actions"]')!,
        { button: 0 },
      );
      expect(screen.getByTestId("move-session-up")).not.toHaveAttribute("aria-disabled", "true");
      fireEvent.click(screen.getByTestId("move-session-up"));
      expect(saved().custom.ids).toEqual(["conv_c", "conv_d"]);
      expect(rowOrder()).toEqual([...familyRows, "conv_d"]);
      fireEvent.contextMenu(link("conv_c"));
      fireEvent.click(screen.getByTestId("move-session-down"));
      expect(rowOrder()).toEqual(["conv_d", ...familyRows]);
      expect(saved().custom.ids).toEqual(["conv_d", "conv_c"]);

      cleanup();
      renderGrouping();
      expect(rowOrder()).toEqual(["conv_d", "conv_c", "conv_peer"]);
      fireEvent.contextMenu(link("conv_peer"));
      expect(screen.queryByTestId("move-session-up")).toBeNull();
      expect(screen.queryByTestId("move-session-down")).toBeNull();
    },
  );

  it("starts from the order on screen on drop, not hover, and never follows the row's link", async () => {
    renderGrouping();
    expect(rowOrder()).toEqual(["conv_c", "conv_d", "conv_e"]);

    await dropOnto("conv_e", "conv_c", {
      expectTarget: false,
      whileOver: () => {
        expect(rowOrder()).toEqual(["conv_c", "conv_d", "conv_e"]);
        expectLine("conv_c", "top");
      },
    });

    expect(rowOrder()).toEqual(["conv_e", "conv_c", "conv_d"]);
    expect(saved()).toMatchObject({
      ordering: "custom",
      custom: { base: "created", since: 30, ids: ["conv_e", "conv_c", "conv_d"] },
    });
    // dnd-kit hides the drag's trailing click from React; only the next press may navigate.
    const row = link("conv_e").closest("li")!;
    expect(fireEvent.click(row, { detail: 1 })).toBe(false);
    fireEvent.pointerDown(row);
    expect(fireEvent.click(row, { detail: 1 })).toBe(true);
  });

  it("only takes drops from the dragged session's own group, including onto shared rows", async () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ grouping: "status", ordering: "created" }),
    );
    localStorage.setItem("omnigent:session-filter", "all");
    listRows(conv("conv_f", { created_at: 0, updated_at: 0, owner: "other@example.com" }));
    renderGrouping();
    expect(rowOrder()).toEqual(["conv_d", "conv_c", "conv_e", "conv_f"]);
    // A shared row holds a place in the order but can't be moved itself.
    fireEvent.contextMenu(link("conv_f"));
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("move-session-up")).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    await dropOnto("conv_e", "conv_d", {
      expectTarget: false,
      whileOver: () => expect(screen.queryByTestId("session-order-insertion")).toBeNull(),
    });
    expect(rowOrder()).toEqual(["conv_d", "conv_c", "conv_e", "conv_f"]);
    expect(saved().ordering).toBe("created");

    await dropOnto("conv_e", "conv_c", { expectTarget: false });
    expect(rowOrder()).toEqual(["conv_d", "conv_e", "conv_c", "conv_f"]);
    await dropOnto("conv_c", "conv_f", {
      expectTarget: false,
      whileOver: () => expectLine("conv_f", "bottom"),
    });
    expect(rowOrder()).toEqual(["conv_d", "conv_e", "conv_f", "conv_c"]);
  });

  it.each([
    [
      "row menu",
      (id: string) =>
        fireEvent.pointerDown(
          link(id).closest("li")!.querySelector('[data-testid="conversation-actions"]')!,
          { button: 0 },
        ),
    ],
    ["right-click menu", (id: string) => fireEvent.contextMenu(link(id))],
  ])("moves a session down and up from its %s", (_menu, openMenu) => {
    // A just-created row has no server id yet, so it is never a Move target.
    listRows(conv("temp:new", { created_at: 99, updated_at: 99, provisional: true }));
    renderGrouping();

    openMenu("conv_c");
    expect(screen.getByTestId("move-session-up")).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByTestId("move-session-down"));
    expect(rowOrder()).toEqual(["conv_d", "conv_c", "conv_e"]);

    openMenu("conv_e");
    expect(screen.getByTestId("move-session-down")).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByTestId("move-session-up"));
    expect(rowOrder()).toEqual(["conv_d", "conv_e", "conv_c"]);
    expect(saved().custom.ids).toEqual(["conv_d", "conv_e", "conv_c"]);
  });
});
