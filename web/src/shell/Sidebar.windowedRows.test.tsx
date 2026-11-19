// The sidebar's session list windowed by the real virtualizer, which the shared
// test setup replaces with a pass-through that mounts every row.
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  projects: [] as string[],
  projectRows: [] as Conversation[],
}));

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock, conversationPage: projectPage } =
    await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useProjects: () => ({ data: mocks.projects.map((name) => ({ id: `p_${name}`, name })) }),
    useProjectSessions: (_project: string, enabled: boolean) =>
      projectPage(enabled ? mocks.projectRows : []),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.unmock("@tanstack/react-virtual");

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { writeProjectFolderCollapse } from "@/lib/projectFolderPaginationPreferences";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { stubVirtualListLayout } from "@/test/virtualListLayout";
import { EXPANDED_PROJECT_SECTIONS_STORAGE_KEY } from "./sidebarNav";

const VIEWPORT_HEIGHT = 400;
const ROW_HEIGHT = 32;
const ROW_PITCH = ROW_HEIGHT + 1;
const SESSION_COUNT = 300;
const WINDOWED = { sidebar_windowed_rows: true };

const sessions = (count: number) =>
  Array.from({ length: count }, (_, index) =>
    conversation(`conv_${index}`, { title: `Session ${index}`, updated_at: 1_000 - index }),
  );
const scrollIntoView = vi.fn();

const scroller = () => document.querySelector<HTMLElement>("nav.overflow-y-auto")!;
const mountedRows = () => document.querySelectorAll("li[data-sidebar-session-id]");
const row = (id: string) =>
  document.querySelector<HTMLElement>(`li[data-sidebar-session-id="${id}"]`);
const scrollToRow = (index: number) =>
  act(() => {
    scroller().scrollTop = index * ROW_PITCH;
    scroller().dispatchEvent(new Event("scroll"));
  });

let restoreLayout: () => void;
beforeAll(() => {
  restoreLayout = stubVirtualListLayout({
    isScroller: (element) =>
      element.tagName === "NAV" && element.classList.contains("overflow-y-auto"),
    viewportHeight: VIEWPORT_HEIGHT,
    rowHeight: ROW_HEIGHT,
    scrollIntoView,
  });
});

afterAll(() => restoreLayout());

beforeEach(() => {
  mocks.projects = [];
  mocks.projectRows = [];
  vi.mocked(useConversations).mockImplementation(() => conversationPage(sessions(SESSION_COUNT)));
});

afterEach(() => {
  fireEvent.mouseUp(document);
  cleanup();
  localStorage.clear();
  scrollIntoView.mockClear();
  vi.unstubAllGlobals();
});

describe("Sidebar windowed session rows", () => {
  it("mounts only the rows near the viewport, and others as the list scrolls", () => {
    renderSidebar({ features: WINDOWED });

    expect(row("conv_0")).not.toBeNull();
    expect(mountedRows().length).toBeLessThan(80);
    scrollToRow(250);
    expect(row("conv_250")).not.toBeNull();
    expect(row("conv_0")).toBeNull();
    expect(mountedRows().length).toBeLessThan(80);
  });

  it("mounts every row with the flag off", () => {
    renderSidebar({ features: {} });

    expect(mountedRows()).toHaveLength(SESSION_COUNT);
  });

  it("scrolls to the open session's row far down the list", async () => {
    renderSidebar({ features: WINDOWED, route: "/c/conv_250", path: "/c/:conversationId" });

    await waitFor(() => {
      expect(scroller().scrollTop).toBeGreaterThan(200 * ROW_PITCH);
      expect(row("conv_250")).not.toBeNull();
    });
    expect(row("conv_0")).toBeNull();
  });

  it("leaves the scroll position alone when the open session's row mounts again", async () => {
    renderSidebar({ features: WINDOWED, route: "/c/conv_0", path: "/c/:conversationId" });
    await waitFor(() => expect(row("conv_0")).not.toBeNull());

    scrollToRow(250);
    expect(row("conv_0")).toBeNull();
    // Row 0 mounts again as overscan, above the viewport.
    scrollToRow(8);
    expect(row("conv_0")).not.toBeNull();
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(scroller().scrollTop).toBe(8 * ROW_PITCH);
  });

  it("keeps a row's open menu mounted while the list scrolls away", () => {
    renderSidebar({ features: WINDOWED });

    // Radix DropdownMenu opens on pointerdown.
    fireEvent.pointerDown(within(row("conv_2")!).getByTestId("conversation-actions"), {
      button: 0,
    });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    scrollToRow(250);

    expect(row("conv_0")).toBeNull();
    expect(row("conv_2")).not.toBeNull();
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("keeps a row being renamed mounted and focused while the list scrolls away", () => {
    renderSidebar({ features: WINDOWED });

    fireEvent.dblClick(screen.getByRole("link", { name: /Session 2$/ }));
    const input = screen.getByTestId("rename-conversation-input");
    expect(input).toHaveFocus();
    scrollToRow(250);

    expect(row("conv_0")).toBeNull();
    expect(screen.getByTestId("rename-conversation-input")).toBe(input);
    expect(input).toHaveFocus();
  });

  it("keeps a dragged row mounted while the list scrolls to a far drop target", async () => {
    renderSidebar({ features: { ...WINDOWED, sidebar_grouping_ui: true } });
    const source = row("conv_2")!;
    const start = { clientX: 50, clientY: 2 * ROW_PITCH + ROW_HEIGHT / 2 };

    // A browser presses the pointer before the mouse sensor's mousedown.
    fireEvent.pointerDown(source, { button: 0, ...start });
    fireEvent.mouseDown(source, { button: 0, ...start });
    await act(async () => {
      fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY + 10 });
    });
    expect(source).toHaveClass("opacity-40");
    scrollToRow(250);

    expect(row("conv_0")).toBeNull();
    expect(row("conv_250")).not.toBeNull();
    expect(row("conv_2")).toBe(source);
    expect(source).toHaveClass("opacity-40");
    const target = { clientX: start.clientX, clientY: ROW_HEIGHT / 2 };
    await act(async () => {
      fireEvent.mouseMove(document, target);
    });
    await act(async () => {
      fireEvent.mouseUp(document, target);
    });

    // A row dragged down lands below its target.
    const { ids } = JSON.parse(localStorage.getItem("omnigent:sidebar-view")!).custom;
    expect(ids.indexOf("conv_2")).toBe(ids.indexOf("conv_250") + 1);
  });

  it("places and reveals rows in a windowed list below another windowed list", async () => {
    writeProjectFolderCollapse(false);
    mocks.projects = ["Example project"];
    mocks.projectRows = Array.from({ length: 60 }, (_, index) =>
      conversation(`proj_${index}`, {
        updated_at: 2_000 - index,
        labels: { omni_project: "Example project" },
      }),
    );
    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage([...mocks.projectRows, ...sessions(SESSION_COUNT)]),
    );
    localStorage.setItem(
      EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
      JSON.stringify(["Example project"]),
    );
    renderSidebar({ features: WINDOWED, route: "/c/conv_250", path: "/c/:conversationId" });

    await waitFor(() => {
      const revealed = row("conv_250")!.getBoundingClientRect();
      expect(revealed.top).toBeGreaterThanOrEqual(0);
      expect(revealed.bottom).toBeLessThanOrEqual(VIEWPORT_HEIGHT);
    });
    const list = row("conv_250")!.parentElement!;
    const listOffset = list.getBoundingClientRect().top + scroller().scrollTop;
    expect(listOffset).toBeGreaterThan(59 * ROW_PITCH);
    act(() => {
      scroller().scrollTop = listOffset + 100 * ROW_PITCH;
      scroller().dispatchEvent(new Event("scroll"));
    });
    expect(row("conv_100")!.getBoundingClientRect().top).toBe(0);
    expect(row("conv_250")).toBeNull();
  });

  it("loads the next page from the end of the windowed list and windows its rows", async () => {
    const observed: Element[] = [];
    let loadMore: IntersectionObserverCallback | undefined;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(callback: IntersectionObserverCallback) {
          loadMore = callback;
        }
        observe = (target: Element) => observed.push(target);
        unobserve = vi.fn();
        disconnect = vi.fn();
        takeRecords = () => [];
      },
    );
    const fetchNextPage = vi.fn();
    vi.mocked(useConversations).mockImplementation(() => ({
      ...conversationPage(sessions(SESSION_COUNT)),
      hasNextPage: true,
      fetchNextPage,
    }));
    const view = renderSidebar({ features: WINDOWED });

    const list = row("conv_0")!.parentElement!;
    expect(observed.at(-1)!.compareDocumentPosition(list)).toBe(Node.DOCUMENT_POSITION_PRECEDING);
    await act(async () => {
      loadMore?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as never);
    });
    expect(fetchNextPage).toHaveBeenCalled();

    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage(sessions(SESSION_COUNT + 30)),
    );
    view.setOpen(true);
    scrollToRow(SESSION_COUNT + 20);
    expect(row(`conv_${SESSION_COUNT + 29}`)).not.toBeNull();
    expect(mountedRows().length).toBeLessThan(80);
  });

  it.each([
    { collapse: false, position: "absolute" },
    { collapse: true, position: "" },
  ])(
    "windows an expanded project folder's rows unless Show more pages them (collapse: $collapse)",
    ({ collapse, position }) => {
      writeProjectFolderCollapse(collapse);
      mocks.projects = ["Example project"];
      mocks.projectRows = sessions(SESSION_COUNT).map((session) => ({
        ...session,
        labels: { omni_project: "Example project" },
      }));
      vi.mocked(useConversations).mockImplementation(() => conversationPage(mocks.projectRows));
      localStorage.setItem(
        EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
        JSON.stringify(["Example project"]),
      );
      renderSidebar({ features: WINDOWED });

      const folderRows = screen
        .getByText("Example project")
        .closest("section")!
        .querySelectorAll<HTMLElement>("li[data-sidebar-session-id]");
      expect(folderRows.length).toBeLessThan(80);
      // The Show more slide animates each appended row in normal flow.
      expect(folderRows[0]!.style.position).toBe(position);
    },
  );
});
