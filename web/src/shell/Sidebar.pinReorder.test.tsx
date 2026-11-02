import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as capabilities from "@/lib/CapabilitiesContext";
import * as serverOrigin from "@/lib/serverOrigin";
import * as gridSessions from "@/grid/GridSessionsContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { readSidebarViewPreferences } from "@/lib/sidebarViewPreferences";

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
  moveToProject: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  const { useMutation } = await import("@tanstack/react-query");
  const { PIN_WRITE_MUTATION_KEY } = await import("@/lib/sessionListCache");
  return {
    ...conversationHooksMock(),
    useProjects: vi.fn(() => ({ data: [] })),
    useProjectSessions: vi.fn(() => conversationPage([])),
    useMoveToProject: () => ({ mutate: mocks.moveToProject }),
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

import { useConversations, useProjects, useProjectSessions } from "@/hooks/useConversations";
import { Toaster } from "@/components/ui/sonner";
import { toast } from "sonner";

const ROW_HEIGHT = 30;

// Stack rows and their enclosing project folders; jsdom has no layout.
function stubRowRects() {
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
    this: Element,
  ) {
    if (this.getAttribute("data-grid-drop-edge") === "left") return new DOMRect(300, 0, 200, 200);
    const rows = [...document.querySelectorAll("li[data-sidebar-session-id]")];
    const projectFolder = this.querySelector("[data-project-order-name]");
    const indices = rows.flatMap((row, index) =>
      row === this || (projectFolder && this.contains(row)) ? [index] : [],
    );
    if (!indices.length) return new DOMRect(0, 0, 0, 0);
    return new DOMRect(0, indices[0] * ROW_HEIGHT, 200, indices.length * ROW_HEIGHT);
  });
}

function rowCenter(id: string) {
  const rows = [...document.querySelectorAll("li[data-sidebar-session-id]")];
  const index = rows.findIndex((row) => row.getAttribute("data-sidebar-session-id") === id);
  return { clientX: 50, clientY: index * ROW_HEIGHT + ROW_HEIGHT / 2 };
}

async function hoverOnto(sourceId: string, targetId: string) {
  const source = screen.getByRole("link", { name: sourceId }).closest("li")!;
  const start = rowCenter(sourceId);
  const target = rowCenter(targetId);
  fireEvent.mouseDown(source, { button: 0, ...start });
  fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
  await act(async () => {
    fireEvent.mouseMove(document, target);
  });
  return target;
}

async function dropOnto(sourceId: string, targetId: string, { expectTarget = true } = {}) {
  const target = await hoverOnto(sourceId, targetId);
  expect(screen.queryByTestId("pin-order-insertion") !== null).toBe(expectTarget);
  await act(async () => {
    fireEvent.mouseUp(document, target);
  });
}

beforeEach(() => {
  localStorage.clear();
  mocks.filterHonored = true;
  vi.mocked(useProjectSessions).mockReturnValue(conversationPage([]));
  vi.mocked(useProjects).mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
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
  vi.useRealTimers();
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

describe("custom session ordering", () => {
  const rowIds = () =>
    [...document.querySelectorAll("li[data-sidebar-session-id]")].map((row) =>
      row.getAttribute("data-sidebar-session-id"),
    );
  beforeEach(() => {
    mocks.pinned = [];
    vi.spyOn(capabilities, "useServerInfo").mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      features: { sidebar_grouping_ui: true },
    });
  });

  it.each([
    { sourceProject: null, pinned: false, archived: false },
    { sourceProject: "Other", pinned: false, archived: false },
    { sourceProject: "Target", pinned: true, archived: false },
    { sourceProject: "Target", pinned: false, archived: false },
    { sourceProject: "Target", pinned: false, archived: true },
  ])("routes a drop over a project member: %j", async ({ sourceProject, pinned, archived }) => {
    vi.mocked(useProjects).mockReturnValue({
      data: ["Target", "Other"].map((name) => ({ id: null, name })),
    } as unknown as ReturnType<typeof useProjects>);
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Target", "Other"]));
    const source = conv("source", {
      archived,
      labels: sourceProject ? { omni_project: sourceProject } : {},
    });
    const target = conv("target", { archived, labels: { omni_project: "Target" } });
    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage(archived ? [] : [source, target]),
    );
    if (archived) {
      localStorage.setItem("omnigent:session-filter", "archived");
      vi.mocked(useProjectSessions).mockImplementation((name) =>
        conversationPage(name === "Target" ? [source, target] : []),
      );
    }
    if (pinned) mocks.pinned = [source];
    mocks.pinFn.mockResolvedValue({});
    renderSidebar();

    const point = await hoverOnto("source", "target");
    await act(async () => {
      fireEvent.mouseUp(document, point);
    });
    if (sourceProject === "Target" && !pinned) {
      expect(mocks.moveToProject).not.toHaveBeenCalled();
      expect(readSidebarViewPreferences().ordering).toBe("custom");
      expect(rowIds()).toEqual(["source", "target"]);
      expect(readSidebarViewPreferences().customOrder?.ids).toEqual(["source", "target"]);
    } else {
      expect(mocks.moveToProject).toHaveBeenCalledExactlyOnceWith({
        id: "source",
        project: "Target",
      });
      expect(readSidebarViewPreferences().ordering).toBe("updated");
      if (pinned)
        expect(mocks.pinFn).toHaveBeenCalledExactlyOnceWith({ id: "source", pinned: false });
    }
  });

  it.each([
    { archived: false, owner: null },
    { archived: false, owner: "someone-else" },
    { archived: true, owner: null },
  ])("allows only live sessions into a grid: %j", async ({ archived, owner }) => {
    vi.spyOn(serverOrigin, "isCurrentServerLocal").mockReturnValue(false);
    localStorage.setItem("omnigent:session-filter", archived ? "archived" : "all");
    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage([conv("source", { archived, owner })]),
    );
    const split = vi.fn();
    vi.spyOn(gridSessions, "useGridSessions").mockReturnValue({
      enabled: true,
      desktop: true,
      grids: [],
      conversationId: "current",
      split,
    } as unknown as NonNullable<ReturnType<typeof gridSessions.useGridSessions>>);
    render(<div data-grid-session="current" />);
    renderSidebar();
    await hoverOnto("source", "source");
    const point = { clientX: 350, clientY: 100 };
    await act(async () => {
      fireEvent.mouseMove(document, point);
    });
    expect(screen.queryByTestId("grid-drop-ghost") !== null).toBe(!archived);
    await act(async () => {
      fireEvent.mouseUp(document, point);
    });
    if (archived) expect(split).not.toHaveBeenCalled();
    else expect(split).toHaveBeenCalledExactlyOnceWith("current", "source", "left");
    expect(readSidebarViewPreferences().ordering).toBe("updated");
  });

  it.each(["drop", "cancel"])(
    "prevents native link navigation after a same-row %s",
    async (finish) => {
      renderSidebar({ route: "/c/conv_d" });
      const link = screen.getByRole("link", { name: "conv_c" });
      const point = await hoverOnto("conv_c", "conv_c");
      vi.useFakeTimers();
      await act(async () => {
        if (finish === "cancel") fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
        fireEvent.mouseUp(document, point);
        expect(fireEvent.click(link, { detail: 1 })).toBe(false);
      });
      expect(readSidebarViewPreferences().ordering).toBe("updated");

      // Flush dnd-kit's pending listener cleanup without depending on its delay.
      await act(async () => {
        await vi.runOnlyPendingTimersAsync();
      });
      vi.useRealTimers();
      const defaultPrevented = vi.fn((event: Event) => event.defaultPrevented);
      link.addEventListener("click", defaultPrevented, { once: true });
      fireEvent.pointerDown(link, { button: 0 });
      fireEvent.click(link, { detail: 1 });
      expect(defaultPrevented).toHaveReturnedWith(false);
    },
  );

  it.each(["created", "updated"] as const)(
    "switches %s to Custom only on drop and keeps the saved order across updates and reload",
    async (ordering) => {
      localStorage.setItem("omnigent:sidebar-view", JSON.stringify({ ordering }));
      let rows = [
        conv("conv_a", { created_at: 30, updated_at: 10 }),
        conv("conv_b", { created_at: 20, updated_at: 20 }),
        conv("conv_c", { created_at: 10, updated_at: 30 }),
      ];
      vi.mocked(useConversations).mockImplementation(() => conversationPage(rows));
      renderSidebar();
      const initial =
        ordering === "created" ? ["conv_a", "conv_b", "conv_c"] : ["conv_c", "conv_b", "conv_a"];
      expect(rowIds()).toEqual(initial);
      const target = await hoverOnto(initial[2], initial[0]);
      expect(rowIds()).toEqual(initial);
      expect(document.querySelector('[data-session-order-over="true"]')).toHaveAttribute(
        "data-sidebar-session-id",
        initial[0],
      );
      expect(readSidebarViewPreferences().ordering).toBe(ordering);
      await act(async () => {
        fireEvent.mouseUp(document, target);
      });
      const reordered = [initial[2], initial[0], initial[1]];
      expect(rowIds()).toEqual(reordered);
      expect(readSidebarViewPreferences()).toMatchObject({
        ordering: "custom",
        customOrder: { baseOrdering: ordering },
      });

      cleanup();
      rows = [
        conv("new_session", { created_at: Math.floor(Date.now() / 1000) + 1, updated_at: 1000 }),
        ...rows.map((row) => ({ ...row, updated_at: 1000 - row.updated_at })),
        conv("older_page", { created_at: 1, updated_at: 1 }),
      ];
      renderSidebar();
      expect(rowIds()).toEqual(["new_session", ...reordered, "older_page"]);
      expect(mocks.pinFn).not.toHaveBeenCalled();
      expect(mocks.reorderPins).not.toHaveBeenCalled();
    },
  );

  it.each(["cancel", "outside", "self", "other group"])(
    "ignores a %s drop",
    async (destination) => {
      localStorage.setItem(
        "omnigent:sidebar-view",
        JSON.stringify({ ordering: "updated", grouping: "status" }),
      );
      vi.mocked(useConversations).mockImplementation(() =>
        conversationPage([conv("conv_a"), conv("conv_b"), conv("working", { status: "running" })]),
      );
      renderSidebar();
      const initial = rowIds();
      const target = await hoverOnto(
        "conv_a",
        destination === "self" ? "conv_a" : destination === "other group" ? "working" : "conv_b",
      );
      await act(async () => {
        if (destination === "cancel")
          fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
        if (destination === "outside")
          fireEvent.mouseMove(document, { clientX: 500, clientY: 500 });
      });
      expect(document.querySelector('[data-session-order-over="true"]')).toBeNull();
      await act(async () => {
        fireEvent.mouseUp(document, target);
      });
      expect(rowIds()).toEqual(initial);
      expect(readSidebarViewPreferences().ordering).toBe("updated");
      expect(readSidebarViewPreferences().customOrder).toBeUndefined();
    },
  );

  it("offers personal ordering for shared sessions through the keyboard menu", () => {
    vi.spyOn(serverOrigin, "isCurrentServerLocal").mockReturnValue(false);
    localStorage.setItem("omnigent:session-filter", "shared");
    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage([
        conv("shared_a", { owner: "someone-else", permission_level: 1 }),
        conv("shared_b", { owner: "someone-else", permission_level: 1 }),
      ]),
    );
    renderSidebar();
    expect(rowIds()).toEqual(["shared_b", "shared_a"]);
    const row = screen.getByRole("link", { name: "shared_a" }).closest("li")!;
    fireEvent.keyDown(row.querySelector('[data-testid="conversation-actions"]')!, {
      key: "ArrowDown",
    });
    const move = screen.getByRole("menuitem", { name: "Move up" });
    move.focus();
    fireEvent.keyDown(move, { key: "Enter" });
    expect(rowIds()).toEqual(["shared_a", "shared_b"]);
    expect(readSidebarViewPreferences().ordering).toBe("custom");
    expect(mocks.pinFn).not.toHaveBeenCalled();
  });

  it("ignores saved Custom ordering and row drops while the release flag is off", async () => {
    vi.mocked(capabilities.useServerInfo).mockReturnValue(FALLBACK_SERVER_INFO);
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({
        ordering: "custom",
        customOrder: { baseOrdering: "created", ids: ["conv_c", "conv_d"], startedAt: 100 },
      }),
    );
    renderSidebar();
    expect(rowIds()).toEqual(["conv_d", "conv_c"]);
    const target = await hoverOnto("conv_c", "conv_d");
    expect(document.querySelector('[data-session-order-over="true"]')).toBeNull();
    await act(async () => {
      fireEvent.mouseUp(document, target);
    });
    expect(rowIds()).toEqual(["conv_d", "conv_c"]);
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
    await waitFor(() => expect(pill).not.toBeInTheDocument());
  });
});
