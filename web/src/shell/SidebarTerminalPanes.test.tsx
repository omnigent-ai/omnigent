import type * as CapabilitiesModule from "@/lib/capabilities";
import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseFloatingPanesModule from "./useFloatingPanes";

import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";
import type { TerminalGroup } from "@/lib/terminalGroups";

import { updatePaneSplit } from "./paneSplitStore";
import {
  isFailedPaneStart,
  recordPaneStartFailure,
  resetPaneStartFailuresForTests,
} from "./paneStartFailures";
import { SidebarTerminalPanes } from "./SidebarTerminalPanes";
import type { TerminalPaneSurfaceProps } from "./TerminalPaneSurface";
import {
  pendingTerminalFocus,
  requestTerminalFocus,
  settleTerminalFocus,
} from "./terminalFocusRequest";
import { resetTerminalOpenRequestsForTests, useTerminalOpenRequests } from "./terminalOpenRequest";
import {
  readTerminalGroups,
  resetTerminalGroupsCache,
  TERMINAL_GROUPS_KEY,
  updateTerminalGroups,
} from "./terminalGroupsStore";
import {
  resetTerminalPlacement,
  useMainSplitMembers,
  usePublishMainTerminalKeys,
} from "./terminalPlacement";

const lifecycle = vi.hoisted(() => ({ mounted: [] as string[], unmounted: [] as string[] }));
const listing = vi.hoisted(() => ({ ids: ["a", "b", "c"] }));
// Each mounted pane's answer to "take the keyboard now?" as its terminal connects.
const connectFocus = vi.hoisted(() => new Map<string, () => boolean>());

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: () => ({
    terminals: listing.ids.map((id) => ({ id, name: "zsh", session: id, running: true })),
    listCompleteAt: 1,
    isFetching: false,
  }),
  useCreateTerminal: () => ({ launch: vi.fn(), isPending: false }),
  useDeleteTerminal: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: () => ({ data: { terminals: ["zsh"] } }) }));
vi.mock("./useTerminalStatuses", () => ({
  useTerminalStatuses: () => ({ setTerminalConnectionState: vi.fn(), markTerminalActive: vi.fn() }),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => false }));
const floatingPanes = vi.hoisted(() => ({ shown: false }));
vi.mock("./useFloatingPanes", async (importOriginal) => ({
  ...(await importOriginal<typeof UseFloatingPanesModule>()),
  useFloatingPanesShown: () => floatingPanes.shown,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  // Every release feature but gadgets, which keep the classic sidebar instead of these panes.
  isFeatureEnabled: (_info: unknown, key: string) => key !== "gadgets",
}));
// Each pane's terminal, reporting what the sidebar asks of it.
vi.mock("./TerminalPaneSurface", () => ({
  TerminalPaneSurface: ({
    terminal,
    active,
    focusOnConnect,
    focusRequest,
  }: TerminalPaneSurfaceProps) => {
    connectFocus.set(terminal.id, focusOnConnect);
    useEffect(() => {
      lifecycle.mounted.push(terminal.id);
      return () => {
        lifecycle.unmounted.push(terminal.id);
      };
    }, [terminal.id]);
    return (
      <div
        data-testid={`terminal-${terminal.id}`}
        data-active={String(active)}
        data-focus-request={focusRequest ?? ""}
      >
        <div {...{ [TERMINAL_SURFACE_ATTRIBUTE]: "" }}>
          <textarea aria-label={`${terminal.id} terminal`} />
        </div>
        <input aria-label={`${terminal.id} find`} />
      </div>
    );
  },
}));

const PAIR: TerminalGroup = {
  id: "g_ab",
  split: {
    layout: { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:b"] },
    focusedKey: "terminal:b",
  },
};

const NO_SHELLS: string[] = [];

let session = 0;
const nextSession = () => `conv_sidebar_panes_${++session}`;

/** The sidebar as the workspace mounts it: beside a main view that shows splits, unless not. */
function Sidebar({
  conversationId,
  shownKey,
  mountedKeys = NO_SHELLS,
  mainSplitsShown = true,
  readOnly = false,
  autoFocusKey = null,
}: {
  conversationId: string;
  shownKey: string | null;
  mountedKeys?: string[];
  mainSplitsShown?: boolean;
  readOnly?: boolean;
  autoFocusKey?: string | null;
}) {
  const mainSplitMembers = useMainSplitMembers(conversationId, mainSplitsShown);
  return (
    <SidebarTerminalPanes
      conversationId={conversationId}
      shownKey={shownKey}
      mountedKeys={mountedKeys}
      readOnly={readOnly}
      visible
      autoFocusKey={autoFocusKey}
      mainSplitMembers={mainSplitMembers}
    />
  );
}

beforeEach(() => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
});

afterEach(() => {
  floatingPanes.shown = false;
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetTerminalGroupsCache();
  resetTerminalPlacement();
  resetPaneStartFailuresForTests();
  lifecycle.mounted.length = 0;
  lifecycle.unmounted.length = 0;
  listing.ids = ["a", "b", "c"];
  connectFocus.clear();
  resetTerminalOpenRequestsForTests();
});

describe("SidebarTerminalPanes", () => {
  it.each([
    { floating: false, target: true },
    { floating: true, target: false },
  ])(
    "takes a tab dragged over its split only without floating panes ($floating)",
    async ({ floating, target }) => {
      floatingPanes.shown = floating;
      const id = nextSession();
      updateTerminalGroups(id, () => [PAIR]);
      const { container } = render(<Sidebar conversationId={id} shownKey="terminal:a" />);
      await screen.findByTestId("terminal-a");
      // With floating panes the main view's split takes the tab, so the sidebar's never claims it.
      expect(container.querySelector(`[data-split-panes="${id}"]`) !== null).toBe(target);
    },
  );

  it("keeps a member whose shell didn't start in its group, saying why, though the list never has it", async () => {
    const id = nextSession();
    recordPaneStartFailure(id, "terminal:x", { terminal: "zsh", reason: "runner offline" });
    const withFailed: TerminalGroup = {
      id: "g_ax",
      split: {
        layout: { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:x"] },
        focusedKey: "terminal:x",
      },
    };
    updateTerminalGroups(id, () => [withFailed]);
    render(<Sidebar conversationId={id} shownKey="terminal:a" />);
    expect(await screen.findByTestId("terminal-start-failed")).toHaveTextContent("runner offline");
    expect(readTerminalGroups(id).map((group) => group.split.layout)).toEqual([
      withFailed.split.layout,
    ]);
  });

  it("ends a group's failed starts once no member has a running shell", () => {
    const id = nextSession();
    for (const key of ["terminal:x", "terminal:y"]) {
      recordPaneStartFailure(id, key, { terminal: "zsh", reason: "runner offline" });
    }
    // The list never had "terminal:gone": its shell ended while it was away.
    updateTerminalGroups(id, () => [
      {
        id: "g_failed",
        split: {
          layout: {
            axis: "columns",
            ratio: 0.5,
            children: [
              "terminal:gone",
              { axis: "rows", ratio: 0.5, children: ["terminal:x", "terminal:y"] },
            ],
          },
          focusedKey: "terminal:gone",
        },
      },
    ]);
    render(<Sidebar conversationId={id} shownKey={null} />);
    expect(readTerminalGroups(id)).toEqual([]);
    expect(isFailedPaneStart(id, "terminal:x")).toBe(false);
    expect(isFailedPaneStart(id, "terminal:y")).toBe(false);
  });

  it("keeps a shell that isn't selected mounted, without the keyboard", async () => {
    const id = nextSession();
    const { rerender } = render(
      <Sidebar conversationId={id} shownKey="terminal:a" mountedKeys={["terminal:b"]} />,
    );
    expect((await screen.findByTestId("terminal-a")).dataset.active).toBe("true");
    expect(screen.getByTestId("terminal-b").dataset.active).toBe("false");

    rerender(<Sidebar conversationId={id} shownKey="terminal:b" mountedKeys={["terminal:a"]} />);
    expect(screen.getByTestId("terminal-a").dataset.active).toBe("false");
    expect(screen.getByTestId("terminal-b").dataset.active).toBe("true");
    expect(lifecycle.unmounted).toEqual([]);
  });

  it("makes the selected member of a split its focused pane", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    render(<Sidebar conversationId={id} shownKey="terminal:a" />);
    expect((await screen.findByTestId("terminal-a")).dataset.active).toBe("true");
    expect(screen.getByTestId("terminal-b").dataset.active).toBe("false");
    expect(readTerminalGroups(id)[0].split.focusedKey).toBe("terminal:a");
  });

  it("keeps a focus move inside the split, such as to a new pane, until another member is selected", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [
      { ...PAIR, split: { ...PAIR.split, focusedKey: "terminal:a" } },
    ]);
    const { rerender } = render(<Sidebar conversationId={id} shownKey="terminal:a" />);
    await screen.findByTestId("terminal-b");
    act(() =>
      updateTerminalGroups(id, (groups) =>
        groups.map((group) => ({ ...group, split: { ...group.split, focusedKey: "terminal:b" } })),
      ),
    );
    expect(screen.getByTestId("terminal-b").dataset.active).toBe("true");
    expect(readTerminalGroups(id)[0].split.focusedKey).toBe("terminal:b");
    rerender(<Sidebar conversationId={id} shownKey="terminal:b" />);
    rerender(<Sidebar conversationId={id} shownKey="terminal:a" />);
    expect(screen.getByTestId("terminal-a").dataset.active).toBe("true");
    expect(readTerminalGroups(id)[0].split.focusedKey).toBe("terminal:a");
  });

  it("keeps a live shell in its group when the list refills after an empty one, before a new answer", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    const { rerender } = render(<Sidebar conversationId={id} shownKey="terminal:a" />);
    await screen.findByTestId("terminal-b");
    // The list empties, as on an agent switch, and refills one shell at a time while the last
    // complete answer is still the one from before.
    listing.ids = [];
    rerender(<Sidebar conversationId={id} shownKey="terminal:a" />);
    listing.ids = ["a"];
    rerender(<Sidebar conversationId={id} shownKey="terminal:a" />);
    expect(readTerminalGroups(id).map((group) => group.split.layout)).toEqual([PAIR.split.layout]);
  });

  it("hands the keyboard to the pane beside a shell that ended, if the shell's pane had it", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    const opened = vi.fn();
    renderHook(() => useTerminalOpenRequests(id, opened));
    const { rerender } = render(<Sidebar conversationId={id} shownKey="terminal:b" />);
    await screen.findByTestId("terminal-a");
    act(() => screen.getByLabelText("b terminal").focus());
    // b's shell exits on its own: its row leaves the list, and the split goes with it.
    listing.ids = ["a", "c"];
    rerender(<Sidebar conversationId={id} shownKey="terminal:b" />);
    expect(readTerminalGroups(id)).toEqual([]);
    expect(opened).toHaveBeenCalledWith({ key: "terminal:a", focus: true });
    expect(pendingTerminalFocus(id, "rail")?.tabKey).toBe("terminal:a");
  });

  it("leaves the keyboard where it is when a split's shell ends while its pane didn't have it", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    const opened = vi.fn();
    renderHook(() => useTerminalOpenRequests(id, opened));
    const outside = document.body.appendChild(document.createElement("input"));
    try {
      const { rerender } = render(<Sidebar conversationId={id} shownKey="terminal:b" />);
      await screen.findByTestId("terminal-a");
      act(() => outside.focus());
      listing.ids = ["a", "c"];
      rerender(<Sidebar conversationId={id} shownKey="terminal:b" />);
      expect(readTerminalGroups(id)).toEqual([]);
      expect(document.activeElement).toBe(outside);
      expect(opened).not.toHaveBeenCalled();
      expect(pendingTerminalFocus(id, "rail")).toBeNull();
    } finally {
      outside.remove();
    }
  });

  it("hands no keyboard to a saved split's other shell where splits show as separate tabs", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    const opened = vi.fn();
    renderHook(() => useTerminalOpenRequests(id, opened));
    // A viewer sees a and b as their own tabs; b is shown, alone, and has the keyboard.
    const { rerender } = render(<Sidebar conversationId={id} shownKey="terminal:b" readOnly />);
    await screen.findByTestId("terminal-b");
    expect(screen.queryByTestId("terminal-a")).toBeNull();
    act(() => screen.getByLabelText("b terminal").focus());
    listing.ids = ["a", "c"];
    rerender(<Sidebar conversationId={id} shownKey="terminal:b" readOnly />);
    // Where b's tab closing lands is the tab history's call, not a's.
    expect(opened).not.toHaveBeenCalled();
    expect(pendingTerminalFocus(id, "rail")).toBeNull();
  });

  it("never mounts a shell the main view's split owns, even in a saved group", async () => {
    const id = nextSession();
    updatePaneSplit(id, () => ({
      layout: { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:c"] },
      focusedKey: "terminal:a",
    }));
    updateTerminalGroups(id, () => [PAIR]);
    render(<Sidebar conversationId={id} shownKey="terminal:a" mountedKeys={["terminal:b"]} />);
    expect(await screen.findByText("This shell is open in the Terminal view.")).toBeVisible();
    expect(screen.queryByTestId("terminal-a")).toBeNull();
    expect(lifecycle.mounted).not.toContain("a");
    expect(readTerminalGroups(id)).toEqual([]);
  });

  it("mounts a saved split's shell where the main view can't show that split", async () => {
    const id = nextSession();
    updatePaneSplit(id, () => ({
      layout: { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:c"] },
      focusedKey: "terminal:a",
    }));
    // The shell keeps its tab there, so selecting it must show it, not where it isn't.
    render(
      <Sidebar
        conversationId={id}
        shownKey="terminal:c"
        mountedKeys={["terminal:c"]}
        mainSplitsShown={false}
      />,
    );
    expect(await screen.findByTestId("terminal-c")).toBeVisible();
    expect(screen.queryByText("This shell is open in the Terminal view.")).toBeNull();
  });

  it("leaves out a shell the main view shows on its own", async () => {
    const id = nextSession();
    renderHook(() => usePublishMainTerminalKeys(id, ["terminal:b"]));
    render(<Sidebar conversationId={id} shownKey="terminal:b" mountedKeys={["terminal:a"]} />);
    expect(await screen.findByText("This shell is open in the Terminal view.")).toBeVisible();
    expect(lifecycle.mounted).toEqual(["a"]);
  });

  it("places a group another window saved before any of it mounts", async () => {
    const id = nextSession();
    updatePaneSplit(id, () => ({
      layout: { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:c"] },
      focusedKey: "terminal:a",
    }));
    render(<Sidebar conversationId={id} shownKey="terminal:b" />);
    await screen.findByTestId("terminal-b");
    act(() => {
      window.localStorage.setItem(
        TERMINAL_GROUPS_KEY,
        JSON.stringify({ sessions: { [id]: [PAIR] } }),
      );
      window.dispatchEvent(new StorageEvent("storage", { key: TERMINAL_GROUPS_KEY }));
    });
    expect(screen.queryByTestId("terminal-a")).toBeNull();
    expect(lifecycle.mounted).not.toContain("a");
    expect(readTerminalGroups(id)).toEqual([]);
  });

  it("offers a lone shell's Split in its row", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 400,
      top: 48,
      width: 400,
      height: 600,
      right: 800,
      bottom: 648,
    } as DOMRect);
    const id = nextSession();
    render(<Sidebar conversationId={id} shownKey="terminal:a" />);
    await screen.findByTestId("terminal-a");
    const row = document.querySelector<HTMLElement>('[data-pane-strip="terminal:a"]')!;
    expect(row.textContent).toContain("a");
    expect([...row.querySelectorAll("button")].map((button) => button.dataset.testid)).toEqual([
      "pane-strip-split-right",
      "pane-strip-split-down",
    ]);
  });

  it("keeps every mounted shell's terminal through switching tabs, splitting and unsplitting", async () => {
    const id = nextSession();
    const mountedKeys = ["terminal:a", "terminal:b", "terminal:c"];
    const { rerender } = render(
      <Sidebar conversationId={id} shownKey="terminal:a" mountedKeys={mountedKeys} />,
    );
    await screen.findByTestId("terminal-c");
    expect([...lifecycle.mounted].sort()).toEqual(["a", "b", "c"]);
    // Another shell's tab, then a split of two of them, then back to one.
    rerender(<Sidebar conversationId={id} shownKey="terminal:c" mountedKeys={mountedKeys} />);
    act(() => updateTerminalGroups(id, () => [PAIR]));
    rerender(<Sidebar conversationId={id} shownKey="terminal:b" mountedKeys={mountedKeys} />);
    act(() => updateTerminalGroups(id, () => []));
    // Another kind of tab shows: every shell waits hidden.
    rerender(<Sidebar conversationId={id} shownKey={null} mountedKeys={mountedKeys} />);
    rerender(<Sidebar conversationId={id} shownKey="terminal:a" mountedKeys={mountedKeys} />);
    expect(lifecycle.unmounted).toEqual([]);
    expect(lifecycle.mounted).toHaveLength(3);
  });

  it("gives a shell opened by hand the keyboard as it connects, though focus was in the chat", () => {
    const composer = document.body.appendChild(document.createElement("textarea"));
    onTestFinished(() => composer.remove());
    composer.focus();
    render(
      <Sidebar conversationId={nextSession()} shownKey="terminal:a" autoFocusKey="terminal:a" />,
    );
    expect(document.activeElement).toBe(composer);
    expect(connectFocus.get("a")?.()).toBe(true);
  });

  it("leaves the keyboard where the person moved it before a shell opened by hand connects", () => {
    const composer = document.body.appendChild(document.createElement("textarea"));
    const elsewhere = document.body.appendChild(document.createElement("input"));
    onTestFinished(() => {
      composer.remove();
      elsewhere.remove();
    });
    composer.focus();
    render(
      <Sidebar conversationId={nextSession()} shownKey="terminal:a" autoFocusKey="terminal:a" />,
    );
    elsewhere.focus();
    expect(connectFocus.get("a")?.()).toBe(false);
  });

  it("hands an explicit focus request to the requested member's pane only", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    render(<Sidebar conversationId={id} shownKey="terminal:b" />);
    await screen.findByTestId("terminal-a");
    act(() => requestTerminalFocus({ conversationId: id, surface: "rail", tabKey: "terminal:b" }));
    expect(screen.getByTestId("terminal-b").dataset.focusRequest).not.toBe("");
    expect(screen.getByTestId("terminal-a").dataset.focusRequest).toBe("");
    // Focus landing in its own terminal keeps it for that pane to take.
    act(() => screen.getByLabelText("b terminal").focus());
    expect(screen.getByTestId("terminal-b").dataset.focusRequest).not.toBe("");
    act(() => settleTerminalFocus(id, "rail"));
  });

  it("makes a requested member that isn't selected the focused pane, so it can take the request", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    render(<Sidebar conversationId={id} shownKey="terminal:b" />);
    await screen.findByTestId("terminal-a");
    act(() => requestTerminalFocus({ conversationId: id, surface: "rail", tabKey: "terminal:a" }));
    expect(screen.getByTestId("terminal-a").dataset.active).toBe("true");
    expect(screen.getByTestId("terminal-a").dataset.focusRequest).not.toBe("");
    expect(readTerminalGroups(id)[0].split.focusedKey).toBe("terminal:a");
    act(() => settleTerminalFocus(id, "rail"));
    expect(screen.getByTestId("terminal-a").dataset.active).toBe("true");
  });

  it("cancels a waiting request when the person moves into Find or another pane first", async () => {
    const id = nextSession();
    updateTerminalGroups(id, () => [PAIR]);
    render(<Sidebar conversationId={id} shownKey="terminal:b" />);
    await screen.findByTestId("terminal-b");
    act(() => requestTerminalFocus({ conversationId: id, surface: "rail", tabKey: "terminal:b" }));
    act(() => screen.getByLabelText("b find").focus());
    expect(screen.getByTestId("terminal-b").dataset.focusRequest).toBe("");

    act(() => requestTerminalFocus({ conversationId: id, surface: "rail", tabKey: "terminal:b" }));
    act(() => screen.getByLabelText("a terminal").focus());
    expect(screen.getByTestId("terminal-b").dataset.focusRequest).toBe("");
  });

  describe("zoom", () => {
    const paneOf = (id: string) =>
      screen.getByTestId(`terminal-${id}`).closest<HTMLElement>("[data-terminal-pane]")!;
    const zoomFrom = (id: string) => {
      const input = screen.getByLabelText(`${id} terminal`);
      act(() => input.focus());
      fireEvent.keyDown(input, { key: "Enter", code: "Enter", shiftKey: true, metaKey: true });
    };

    it("fills the view with the zoomed pane, keeping the other's terminal at its size", async () => {
      const id = nextSession();
      updateTerminalGroups(id, () => [PAIR]);
      render(<Sidebar conversationId={id} shownKey="terminal:b" />);
      await screen.findByTestId("terminal-a");
      zoomFrom("b");

      expect(paneOf("b").dataset.paneZoomed).toBe("");
      expect(paneOf("b").style.width).toBe("100%");
      expect(paneOf("a").dataset.paneHidden).toBe("");
      expect(paneOf("a").style.width).toBe("50%");
      expect(screen.getByRole("button", { name: "Restore Panes" })).toBeInTheDocument();
      expect(lifecycle.unmounted).toEqual([]);
    });

    it("unzooms from the row's Restore and hands the keyboard back to the pane", async () => {
      const id = nextSession();
      updateTerminalGroups(id, () => [PAIR]);
      render(<Sidebar conversationId={id} shownKey="terminal:b" />);
      await screen.findByTestId("terminal-a");
      zoomFrom("b");
      const restore = screen.getByRole("button", { name: "Restore Panes" });
      act(() => restore.focus());
      fireEvent.click(restore);

      expect(screen.queryByRole("button", { name: "Restore Panes" })).toBeNull();
      expect(paneOf("a").dataset.paneHidden).toBeUndefined();
      expect(screen.getByTestId("terminal-b").dataset.focusRequest).not.toBe("");
      act(() => settleTerminalFocus(id, "rail"));
    });
  });
});
