// A workspace sidebar hosted with no chat session: the shared frame, tab model and tab
// actions on their own, as a page that only opens links hosts them.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GitPullRequestIcon } from "lucide-react";
import { useSyncExternalStore } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { registeredPaletteCommands } from "@/test/paletteCommands";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { commandRegionProps } from "../commandContext";
import { useTabStripWidth } from "../workspaceTabs/useTabStripWidth";
import { StripTab, StripTabMenu } from "../workspaceTabs/WorkspaceStripTab";
import type { WorkspacePlace } from "../workspaceTabs/workspacePlace";
import { reorderStripEntries } from "../workspaceTabs/stripReorder";
import { isPinnedTab } from "../workspaceTabs/pinnedTabs";
import { isUnknownTab } from "../workspaceTabs/workspaceTab";
import {
  createWorkspaceTabsStore,
  type WorkspaceTabsStore,
} from "../workspaceTabs/workspaceTabsStore";
import { TAB_STRIP_MOTION } from "./tabStripMotion";

/** How long each phase stays marked: its animation, and the slack for its late start. */
const ENTER_WINDOW = TAB_STRIP_MOTION.enterMs + TAB_STRIP_MOTION.settleSlackMs;
const LEAVE_WINDOW = TAB_STRIP_MOTION.leaveMs + TAB_STRIP_MOTION.settleSlackMs;
const REFLOW_WINDOW = TAB_STRIP_MOTION.reflowMs + TAB_STRIP_MOTION.settleSlackMs;
// dnd-kit's pointer sensor removes its click guard 50ms after a drag; a little longer here.
const DRAG_CLICK_GUARD_MS = 60;
import { useWorkspaceSidebarTabActions } from "./useWorkspaceSidebarTabActions";
import { WorkspaceSidebarFrame, WorkspaceSidebarTabPanel } from "./WorkspaceSidebarFrame";

const pull = (number: number): WorkspacePlace => ({
  kind: "pull",
  url: `https://github.com/example/repo/pull/${number}`,
  view: "summary",
});

function makeStore(): WorkspaceTabsStore {
  let next = 0;
  return createWorkspaceTabsStore({
    scopeId: "test-page",
    resting: "empty",
    initial: {
      tabs: [],
      selectedTabId: null,
      timeline: [],
      cursor: 0,
      closed: [],
    },
    save: null,
    requestLeave: () => Promise.resolve("leave"),
    newTabId: () => `t${++next}`,
  });
}

/** A minimal host: no session, no chat store, no shells. */
function Host({ store, pinsStayPut }: { store: WorkspaceTabsStore; pinsStayPut?: boolean }) {
  const { tabs, selectedTabId, canReopen } = useSyncExternalStore(store.subscribe, store.snapshot);
  const stripWidth = useTabStripWidth(tabs.length, 0);
  const label = (id: string) => {
    const tab = tabs.find((candidate) => candidate.id === id);
    return tab && !isUnknownTab(tab) && tab.place.kind === "pull"
      ? tab.place.url.split("/").pop()!
      : "";
  };
  const actions = useWorkspaceSidebarTabActions({
    store,
    scopeId: tabs.length > 0 || canReopen ? "test-page" : null,
    stripWidth,
    sidebarOpen: tabs.length > 0,
    entryIds: tabs.map((tab) => tab.id),
    selectedEntryId: selectedTabId,
    selectedLabel: selectedTabId ? `#${label(selectedTabId)}` : undefined,
  });
  return (
    <TooltipProvider>
      <aside aria-label="Sidebar" {...commandRegionProps("workspace")} className="flex flex-col">
        <WorkspaceSidebarFrame
          stripWidth={stripWidth}
          tabs={{
            value: selectedTabId ?? "",
            onValueChange: (tabId) => store.select(tabId),
            componentId: "test.sidebar",
            scope: "test-page",
            pinsStayPut,
            onReorder: (activeKey, overKey) => {
              const entries = tabs.map((tab) => ({ key: tab.id, tabIds: [tab.id] }));
              const order = reorderStripEntries(entries, activeKey, overKey);
              if (order) store.reorder(order);
            },
            entries: tabs.map((tab, index) => ({
              key: tab.id,
              pinned: isPinnedTab(tab),
              tab: (
                <StripTab
                  tabId={tab.id}
                  index={index}
                  pinned={isPinnedTab(tab)}
                  title={{
                    label: `#${label(tab.id)}`,
                    tooltip: "",
                    icon: GitPullRequestIcon,
                    connecting: false,
                  }}
                  selected={tab.id === selectedTabId}
                  closable
                  onClose={(source) => actions.closeTab(tab.id, source)}
                  width={stripWidth.width}
                  onKeyDown={(event) => actions.handleTabKeyDown(event, tab.id)}
                  menu={
                    <StripTabMenu
                      items={actions.menuItemsFor(tab.id, index, `#${label(tab.id)}`, true)}
                    />
                  }
                />
              ),
            })),
          }}
        >
          {tabs.map((tab) => (
            <WorkspaceSidebarTabPanel
              key={tab.id}
              tabId={tab.id}
              selected={tab.id === selectedTabId}
            >
              <p>Pull request {label(tab.id)}</p>
            </WorkspaceSidebarTabPanel>
          ))}
        </WorkspaceSidebarFrame>
      </aside>
    </TooltipProvider>
  );
}

const command = (label: string) => {
  const found = registeredPaletteCommands().find((candidate) => candidate.label === label);
  if (!found) throw new Error(`No ⌘K command "${label}"`);
  return found;
};
const stripTab = (number: number) => screen.getByRole("tab", { name: new RegExp(`#${number}`) });

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("a workspace sidebar without a session", () => {
  it("shows a tab per pull request and selects one from the strip", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    expect(stripTab(2)).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Pull request 2")).toBeVisible();
    fireEvent.mouseDown(stripTab(1), { button: 0 });
    expect(stripTab(1)).toHaveAttribute("aria-selected", "true");
  });

  it("closes the selected tab from ⌘K and keeps keyboard focus on the tab selected next", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    stripTab(2).focus();
    act(() => command("Close workspace tab").run());
    await waitFor(() => expect(screen.queryByRole("tab", { name: /#2/ })).toBeNull());
    await waitFor(() => expect(stripTab(1)).toHaveFocus());
  });

  it("leaves no tab once the last closes, and Reopen brings it back", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    fireEvent.click(screen.getByRole("button", { name: "Close #1" }));
    await waitFor(() => expect(screen.queryAllByRole("tab")).toHaveLength(0));
    // The tabs' rows stay bound while a closed tab can come back.
    act(() => command("Reopen closed workspace tab").run());
    await waitFor(() => expect(stripTab(1)).toBeInTheDocument());
  });

  it("offers no New tab or tab list it has no place for", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    const labels = registeredPaletteCommands().map((candidate) => candidate.label);
    expect(labels).not.toContain("New workspace tab");
    expect(labels.some((candidate) => candidate.toLowerCase().includes("search tabs"))).toBe(false);
  });

  it("offers Reopen in a tab's menu only once a closed tab can come back", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    fireEvent.contextMenu(stripTab(1));
    await screen.findByRole("menuitem", { name: /Move left/ });
    expect(screen.queryByRole("menuitem", { name: /Reopen closed tab/ })).toBeNull();
    // ⌘K keeps the command, saying why it can't run.
    expect(command("Reopen closed workspace tab").availability).toEqual({
      available: false,
      reason: "There's no closed workspace tab to reopen.",
    });
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Close #2" }));
    await waitFor(() => expect(screen.queryByRole("tab", { name: /#2/ })).toBeNull());
    fireEvent.contextMenu(stripTab(1));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Reopen closed tab/ }));
    await waitFor(() => expect(stripTab(2)).toBeInTheDocument());
  });

  it("moves a focused tab with ⌥⇧→ and from its menu", async () => {
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    fireEvent.keyDown(stripTab(1), { key: "ArrowRight", altKey: true, shiftKey: true });
    expect(store.tabs().map((tab) => tab.id)).toEqual(["t2", "t1"]);
    fireEvent.contextMenu(stripTab(1));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Move left/ }));
    expect(store.tabs().map((tab) => tab.id)).toEqual(["t1", "t2"]);
  });
});

/** A `prefers-reduced-motion` the test can turn on and off, telling its listeners like a browser. */
function stubReducedMotion() {
  let reduced = false;
  const listeners = new Set<() => void>();
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes("prefers-reduced-motion") && reduced,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  return {
    set(next: boolean) {
      reduced = next;
      act(() => listeners.forEach((listener) => listener()));
    },
    restore() {
      window.matchMedia = original;
    },
  };
}

const ghosts = () => document.querySelectorAll<HTMLElement>("[data-tab-strip-leaving]");
const entryOf = (number: number) =>
  stripTab(number).closest<HTMLElement>("[data-tab-strip-entry]")!;
const tabList = () => screen.getByRole("tablist", { name: "Workspace tabs" });

const TAB_WIDTH = 80;
const stripOrder = () => screen.getAllByRole("tab").map((tab) => tab.textContent);

// jsdom has no layout: lay the strip's tabs out in a row for dnd-kit's collisions.
function stubTabRects() {
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
    this: Element,
  ) {
    // The strip's viewport spans far past its tabs, so no drag nears its edges and scrolls it.
    if (this.hasAttribute("data-tab-strip-viewport")) return new DOMRect(-1000, 0, 3000, 24);
    const entries = [...document.querySelectorAll("[data-tab-strip-entry]")];
    const index = entries.indexOf(this);
    if (index < 0) return new DOMRect(0, 0, 0, 0);
    return new DOMRect(index * TAB_WIDTH, 0, TAB_WIDTH, 24);
  });
}

const centerOf = (number: number) => {
  const index = [...document.querySelectorAll("[data-tab-strip-entry]")].indexOf(entryOf(number));
  return { clientX: index * TAB_WIDTH + TAB_WIDTH / 2, clientY: 12 };
};

describe("dragging a strip tab", () => {
  // dnd-kit swallows clicks briefly after a drag ends; let that pass so a later case's clicks land.
  afterEach(async () => {
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, DRAG_CLICK_GUARD_MS);
        }),
    );
  });

  async function openThree(store: WorkspaceTabsStore) {
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    await act(() => store.open({ place: pull(3) }, { opener: "link" }));
    expect(stripOrder()).toEqual(["#1", "#2", "#3"]);
    // The drag's screen reader nodes live outside the tab list, which holds only tabs.
    expect(tabList().querySelectorAll("[aria-live], [id^='DndDescribedBy']")).toHaveLength(0);
    expect(document.body.querySelector("[aria-live]")).not.toBeNull();
  }

  it("moves the tab to where it's dropped, past the tabs it crossed", async () => {
    const rects = stubTabRects();
    const store = makeStore();
    render(<Host store={store} />);
    await openThree(store);
    const start = centerOf(1);
    fireEvent.mouseDown(stripTab(1), { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX + 10, clientY: 12 });
    await act(async () => {
      fireEvent.mouseMove(document, centerOf(3));
    });
    expect(entryOf(1)).toHaveAttribute("data-tab-dragging");
    await act(async () => {
      fireEvent.mouseUp(document, centerOf(3));
    });
    expect(stripOrder()).toEqual(["#2", "#3", "#1"]);
    expect(stripTab(1)).toHaveAttribute("aria-selected", "true");
    rects.mockRestore();
  });

  it("puts the tab back when Escape cancels the drag", async () => {
    const rects = stubTabRects();
    const store = makeStore();
    render(<Host store={store} />);
    await openThree(store);
    const reorder = vi.spyOn(store, "reorder");
    const start = centerOf(1);
    fireEvent.mouseDown(stripTab(1), { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX + 10, clientY: 12 });
    await act(async () => {
      fireEvent.mouseMove(document, centerOf(3));
    });
    expect(entryOf(1)).toHaveAttribute("data-tab-dragging");
    // A file tab closes on an Escape nothing else handled; this one only ends the drag.
    let escapeHandled: boolean | undefined;
    const fileTabEscape = (event: KeyboardEvent) => {
      escapeHandled = event.defaultPrevented;
    };
    window.addEventListener("keydown", fileTabEscape);
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
    });
    window.removeEventListener("keydown", fileTabEscape);
    expect(escapeHandled).toBe(true);
    await act(async () => {
      fireEvent.mouseUp(document, centerOf(3));
    });
    expect(reorder).not.toHaveBeenCalled();
    expect(stripOrder()).toEqual(["#1", "#2", "#3"]);
    expect(entryOf(1)).not.toHaveAttribute("data-tab-dragging");
    rects.mockRestore();
  });

  describe("where the pinned tabs scroll among the others", () => {
    async function openFourPinningTwo(store: WorkspaceTabsStore) {
      for (const number of [1, 2, 3, 4]) {
        // eslint-disable-next-line no-await-in-loop -- each tab opens after the one before
        await act(() => store.open({ place: pull(number) }, { opener: "link" }));
      }
      act(() => {
        store.setPinned(["t1", "t2"], true);
      });
      expect(document.querySelectorAll("[data-tab-strip-pinned]")).toHaveLength(0);
    }

    /** Drags tab `number` onto tab `onto` and returns its offset there, before dropping it. */
    async function dragOnto(number: number, onto: number) {
      const start = centerOf(number);
      fireEvent.mouseDown(stripTab(number), { button: 0, ...start });
      fireEvent.mouseMove(document, { clientX: start.clientX + 10, clientY: 12 });
      await act(async () => {
        fireEvent.mouseMove(document, centerOf(onto));
      });
      expect(entryOf(number)).toHaveAttribute("data-tab-dragging");
      const offset = entryOf(number).style.transform;
      await act(async () => {
        fireEvent.mouseUp(document, centerOf(onto));
      });
      return offset;
    }

    it("stops a tab dragged toward the pinned tabs at their edge", async () => {
      const rects = stubTabRects();
      const store = makeStore();
      render(<Host store={store} pinsStayPut={false} />);
      await openFourPinningTwo(store);
      const reorder = vi.spyOn(store, "reorder");
      expect(await dragOnto(3, 1)).toBe("translate3d(0px, 0, 0)");
      expect(entryOf(1).style.transform).toBe("");
      expect(reorder).not.toHaveBeenCalled();
      rects.mockRestore();
    });

    it("stops a pinned tab dragged toward the others at the pinned tabs' end", async () => {
      const rects = stubTabRects();
      const store = makeStore();
      render(<Host store={store} pinsStayPut={false} />);
      await openFourPinningTwo(store);
      const reorder = vi.spyOn(store, "reorder");
      expect(await dragOnto(2, 4)).toBe("translate3d(0px, 0, 0)");
      expect(reorder).not.toHaveBeenCalled();
      rects.mockRestore();
    });

    it("still reorders the pinned tabs among themselves", async () => {
      const rects = stubTabRects();
      const store = makeStore();
      render(<Host store={store} pinsStayPut={false} />);
      await openFourPinningTwo(store);
      await dragOnto(1, 2);
      expect(store.tabs().map((tab) => tab.id)).toEqual(["t2", "t1", "t3", "t4"]);
      rects.mockRestore();
    });
  });

  it("only selects a tab pressed without moving, and doesn't drag it", async () => {
    const rects = stubTabRects();
    const store = makeStore();
    render(<Host store={store} />);
    await openThree(store);
    const reorder = vi.spyOn(store, "reorder");
    const start = centerOf(1);
    fireEvent.mouseDown(stripTab(1), { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX + 3, clientY: 12 });
    fireEvent.mouseUp(document, { clientX: start.clientX + 3, clientY: 12 });
    expect(stripTab(1)).toHaveAttribute("aria-selected", "true");
    expect(entryOf(1)).not.toHaveAttribute("data-tab-dragging");
    expect(reorder).not.toHaveBeenCalled();
    rects.mockRestore();
  });
});

describe("the strip's motion", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  async function openThree(store: WorkspaceTabsStore) {
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    await act(() => store.open({ place: pull(3) }, { opener: "link" }));
  }

  it("leaves a closed tab as a ghost that is no tab, control or focus stop, and drops it", async () => {
    vi.useFakeTimers();
    const store = makeStore();
    render(<Host store={store} />);
    await openThree(store);
    act(() => vi.advanceTimersByTime(ENTER_WINDOW));
    const closing = entryOf(2);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Close #2" })));

    expect(screen.queryByRole("tab", { name: /#2/ })).toBeNull();
    expect(ghosts()).toHaveLength(1);
    const ghost = ghosts()[0];
    expect(ghost).toHaveTextContent("#2");
    expect(ghost).toHaveAttribute("aria-hidden", "true");
    expect(ghost).toHaveAttribute("inert");
    expect(ghost.querySelector("button, [role=tab], [tabindex]")).toBeNull();
    // It stands where the closed tab was, between its old neighbours.
    expect(ghost.previousElementSibling).toBe(entryOf(1));
    expect(ghost.nextElementSibling).toBe(entryOf(3));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["#1", "#3"]);
    // The ghost is the closed tab's own element, so CSS eases it from its rendered width.
    expect(ghost).toBe(closing);

    // Arrow keys move between the live tabs only.
    stripTab(1).focus();
    fireEvent.keyDown(stripTab(1), { key: "ArrowRight" });
    // Radix moves roving focus on a timer.
    act(() => vi.advanceTimersByTime(0));
    expect(stripTab(3)).toHaveFocus();

    act(() => vi.advanceTimersByTime(LEAVE_WINDOW));
    expect(ghosts()).toHaveLength(0);
  });

  it("grows in only the tabs added after the strip showed, and eases the others' width", async () => {
    vi.useFakeTimers();
    const store = makeStore();
    render(<Host store={store} />);
    await act(() => store.open({ place: pull(1) }, { opener: "link" }));
    // The first tab arrives in place of the empty strip, so it shows at once.
    expect(entryOf(1)).not.toHaveAttribute("data-tab-entering");
    await act(() => store.open({ place: pull(2) }, { opener: "link" }));
    expect(entryOf(2)).toHaveAttribute("data-tab-entering");
    expect(entryOf(1)).not.toHaveAttribute("data-tab-entering");
    expect(tabList()).toHaveAttribute("data-tab-reflow");

    act(() => vi.advanceTimersByTime(REFLOW_WINDOW));
    expect(tabList()).not.toHaveAttribute("data-tab-reflow");
    act(() => vi.advanceTimersByTime(ENTER_WINDOW - REFLOW_WINDOW));
    expect(entryOf(2)).not.toHaveAttribute("data-tab-entering");
  });

  it("reopens a tab as a live one that grows in, while its ghost still leaves", async () => {
    vi.useFakeTimers();
    const store = makeStore();
    render(<Host store={store} />);
    await openThree(store);
    act(() => vi.advanceTimersByTime(ENTER_WINDOW));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Close #3" })));
    expect(ghosts()).toHaveLength(1);
    act(() => command("Reopen closed workspace tab").run());
    expect(stripTab(3)).toHaveAttribute("aria-selected", "true");
    expect(entryOf(3)).toHaveAttribute("data-tab-entering");
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    act(() => vi.advanceTimersByTime(LEAVE_WINDOW));
    expect(ghosts()).toHaveLength(0);
    act(() => vi.advanceTimersByTime(ENTER_WINDOW - LEAVE_WINDOW));
    expect(entryOf(3)).not.toHaveAttribute("data-tab-entering");
  });

  it("stops every animation the moment Reduce motion turns on, with no tab change", async () => {
    vi.useFakeTimers();
    const reducedMotion = stubReducedMotion();
    try {
      const store = makeStore();
      render(<Host store={store} />);
      await openThree(store);
      // Partway through #3 growing in, #2 closes.
      act(() => vi.advanceTimersByTime(TAB_STRIP_MOTION.enterMs / 2));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Close #2" })));
      expect(ghosts()).toHaveLength(1);
      expect(entryOf(3)).toHaveAttribute("data-tab-entering");
      expect(tabList()).toHaveAttribute("data-tab-reflow");

      reducedMotion.set(true);
      expect(ghosts()).toHaveLength(0);
      expect(entryOf(3)).not.toHaveAttribute("data-tab-entering");
      expect(tabList()).not.toHaveAttribute("data-tab-reflow");

      // While it stays on, tabs come and go without motion.
      await act(() => store.open({ place: pull(4) }, { opener: "link" }));
      expect(entryOf(4)).not.toHaveAttribute("data-tab-entering");
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Close #4" })));
      expect(ghosts()).toHaveLength(0);
      expect(tabList()).not.toHaveAttribute("data-tab-reflow");
    } finally {
      reducedMotion.restore();
    }
  });
});

/** A strip whose entries the test sets directly, for keys that come back while leaving. */
function KeyedStrip({ keys }: { keys: string[] }) {
  const stripWidth = useTabStripWidth(keys.length, 0);
  return (
    <TooltipProvider>
      <WorkspaceSidebarFrame
        stripWidth={stripWidth}
        tabs={{
          value: keys[0] ?? "",
          onValueChange: () => {},
          componentId: "test.keyed",
          scope: "test-keyed",
          entries: keys.map((key, index) => ({
            key,
            tab: (
              <StripTab
                tabId={key}
                index={index}
                title={{ label: key, tooltip: "", icon: GitPullRequestIcon, connecting: false }}
                selected={index === 0}
                closable
                onClose={() => {}}
                width={stripWidth.width}
                onKeyDown={() => {}}
                menu={<StripTabMenu items={[]} />}
              />
            ),
          })),
        }}
      >
        {null}
      </WorkspaceSidebarFrame>
    </TooltipProvider>
  );
}

describe("a tab that comes back while it is leaving", () => {
  afterEach(() => vi.useRealTimers());

  it("is live again on the same element, no longer inert or hidden", () => {
    vi.useFakeTimers();
    const view = render(<KeyedStrip keys={["a", "b"]} />);
    const element = screen.getByRole("tab", { name: "b" }).closest("[data-tab-strip-entry]");
    view.rerender(<KeyedStrip keys={["a"]} />);
    expect(ghosts()[0]).toBe(element);
    expect(element).toHaveAttribute("inert");
    view.rerender(<KeyedStrip keys={["a", "b"]} />);
    expect(ghosts()).toHaveLength(0);
    const back = screen.getByRole("tab", { name: "b" }).closest("[data-tab-strip-entry]");
    expect(back).toBe(element);
    expect(back).not.toHaveAttribute("inert");
    expect(back).not.toHaveAttribute("aria-hidden");
    expect(back).toHaveAttribute("data-tab-entering");
  });

  it("comes back without the keyboard focus it had when it closed, even with no blur", () => {
    vi.useFakeTimers();
    // jsdom never matches `:focus-visible`; here the browser shows focus wherever it is.
    const matches = Element.prototype.matches;
    vi.spyOn(Element.prototype, "matches").mockImplementation(function (
      this: Element,
      selector: string,
    ) {
      if (selector === ":focus-visible") return document.activeElement === this;
      return matches.call(this, selector);
    });
    // A focused trigger that is removed may never blur; stop any blur before React sees it.
    const swallowBlur = (event: FocusEvent) => event.stopImmediatePropagation();
    try {
      const view = render(<KeyedStrip keys={["a", "b"]} />);
      const trigger = screen.getByRole("tab", { name: "b" });
      const element = trigger.closest("[data-tab-strip-entry]");
      act(() => trigger.focus());
      expect(element).toHaveAttribute("data-keyboard-focus", "true");

      window.addEventListener("focusout", swallowBlur, true);
      view.rerender(<KeyedStrip keys={["a"]} />);
      expect(ghosts()[0]).toBe(element);
      expect(element).not.toHaveAttribute("data-keyboard-focus");

      view.rerender(<KeyedStrip keys={["a", "b"]} />);
      const back = screen.getByRole("tab", { name: "b" });
      expect(back.closest("[data-tab-strip-entry]")).toBe(element);
      expect(back).not.toHaveFocus();
      expect(element).not.toHaveAttribute("data-keyboard-focus");
    } finally {
      window.removeEventListener("focusout", swallowBlur, true);
      vi.restoreAllMocks();
    }
  });
});
