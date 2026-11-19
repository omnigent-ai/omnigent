import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { createPortal } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import {
  exitThingyRailFullScreen,
  requestThingyRailTab,
  resetThingyRailRequests,
  useThingyRailRequest,
} from "@/lib/thingyRailTabs";
import {
  createThingy,
  getSessionThingies,
  getThingy,
  getThingyShelf,
  linkThingyToSession,
} from "@/thingies/api/thingyApi";
import { openThingySession } from "@/thingies/host/thingySession";
import {
  ThingyMobileDrawer,
  ThingyNewSessionPanel,
  ThingyRailTabStrip,
  resetThingyTabOrders,
  useThingyRailTabState,
} from "./ThingyRailTabs";
import { renderWithThingies, thingyFixture } from "./testUtils";
import { resetThingyDrafts } from "./thingyDrafts";
import { resetThingyStarts } from "./useAdoptStartedThingySession";
import type * as ThingySessionModule from "@/thingies/host/thingySession";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getSessionThingies: vi.fn(),
  linkThingyToSession: vi.fn(),
  getThingy: vi.fn(),
  getThingyShelf: vi.fn(),
  createThingy: vi.fn(),
}));
vi.mock("@/thingies/host/thingySession", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingySessionModule>()),
  openThingySession: vi.fn(),
}));
// The thingy itself (frame, comments) has its own tests; here it names itself and places its
// controls where the panel tells it.
vi.mock("./ThingyView", () => ({
  ThingyView: ({
    thingy,
    renderToolbar,
    toolbarTarget,
  }: {
    thingy: { name: string };
    renderToolbar: (commentsButton: ReactNode) => ReactNode;
    toolbarTarget: HTMLElement | null;
  }) => {
    const controls = <div data-testid="thingy-controls">{renderToolbar(null)}</div>;
    return (
      <>
        {toolbarTarget ? createPortal(controls, toolbarTarget) : controls}
        <p>shows {thingy.name}</p>
      </>
    );
  },
}));

const BOARD = thingyFixture({ id: "1", name: "Board" });
const REPORT = thingyFixture({ id: "2", name: "Report" });

const showTab = () => undefined;

function Rail({
  conversationId,
  readOnly = false,
  onShow = showTab,
  onReveal,
  mobile = false,
}: {
  conversationId: string;
  readOnly?: boolean;
  /** Returns false to leave the panel on another tab, as a session's restore can. */
  onShow?: () => boolean | void;
  onReveal?: () => void;
  mobile?: boolean;
}) {
  const [showing, setShowing] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const [keptSidebar, setKeptSidebar] = useState(false);
  const [toolbar, setToolbar] = useState<HTMLElement | null>(null);
  const state = useThingyRailTabState(conversationId, {
    readOnly,
    showing,
    onShow: () => {
      if (onShow() !== false) setShowing(true);
    },
    onReveal,
    maximized,
    onMaximizedChange: (next, options) => {
      setMaximized(next);
      setKeptSidebar(Boolean(options?.keepSidebar));
    },
    mobile,
  });
  return (
    <div>
      <p aria-label="full screen">
        {maximized ? (keptSidebar ? "yes, with the sidebar" : "yes") : "no"}
      </p>
      <div aria-label="bar" ref={setToolbar} />
      <ThingyRailTabStrip state={state} active />
      <ul aria-label="new items">
        {state.newItems.map((item) => (
          <li key={item.key}>
            <button type="button" onClick={item.onSelect}>
              {item.label}
            </button>
          </li>
        ))}
      </ul>
      {mobile ? (
        <ThingyMobileDrawer state={state} />
      ) : (
        <section aria-label="content">
          {state.selected
            ? state.render(state.selected, {
                fullScreen: maximized,
                toolbar: maximized ? toolbar : null,
              })
            : "none"}
        </section>
      )}
    </div>
  );
}

beforeEach(() => {
  vi.mocked(getSessionThingies).mockResolvedValue([BOARD, REPORT]);
  vi.mocked(linkThingyToSession).mockResolvedValue([BOARD]);
  vi.mocked(getThingyShelf).mockResolvedValue({ pinned: [], hasOwned: true });
});

afterEach(() => {
  resetThingyRailRequests();
  resetThingyDrafts();
  resetThingyStarts();
  resetThingyTabOrders();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("thingy tabs in a session's side panel", () => {
  it("lists the session's thingies, shows the selected one, and closing it shows the tab next to it", async () => {
    const onShow = vi.fn();
    renderWithThingies(<Rail conversationId="conv_1" onShow={onShow} />);

    fireEvent.click(await screen.findByRole("tab", { name: "Report" }));
    expect(onShow).toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "Report" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");

    fireEvent.click(screen.getByRole("button", { name: "Close Report" }));
    await waitFor(() => expect(linkThingyToSession).toHaveBeenCalledWith("2", "conv_1", false));
    // The tab before it takes its place, not another kind of tab.
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
    // The server's answer replaces the session's list.
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Report" })).toBeNull());
  });

  it("takes a request for its session once the tab exists, and opens the panel", async () => {
    vi.mocked(getSessionThingies).mockResolvedValue([]);
    const onReveal = vi.fn();
    const { queryClient } = renderWithThingies(
      <Rail conversationId="conv_1" onReveal={onReveal} />,
    );
    act(() => requestThingyRailTab("other", "thingy:1"));
    act(() => requestThingyRailTab("conv_1", "thingy:1"));
    await waitFor(() => expect(getSessionThingies).toHaveBeenCalled());
    expect(onReveal).not.toHaveBeenCalled();

    act(() => queryClient.setQueryData(["thingies", "session", "conv_1"], [BOARD]));
    await waitFor(() => expect(onReveal).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
  });

  it("switches to a requested tab again when the panel didn't stay on it, then lets the request go", async () => {
    let calls = 0;
    const onShow = vi.fn(() => {
      calls += 1;
      return calls > 1;
    });
    renderWithThingies(<Rail conversationId="conv_1" onShow={onShow} />);
    await screen.findByRole("tab", { name: "Report" });

    vi.useFakeTimers();
    try {
      act(() => requestThingyRailTab("conv_1", "thingy:2"));
      expect(onShow).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");
      // Kept a moment in case the panel is switched away again, then let go.
      expect(useThingyRailRequest.getState().requested).not.toBeNull();
      act(() => vi.advanceTimersByTime(2000));
      expect(useThingyRailRequest.getState().requested).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens a requested tab in full screen, keeping the sidebar when asked, and leaves it on request", async () => {
    renderWithThingies(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });

    act(() => requestThingyRailTab("conv_1", "thingy:1", { maximize: true, keepSidebar: true }));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes, with the sidebar");
    // In full screen the thingy's controls move into the panel's bar.
    expect(within(screen.getByLabelText("bar")).getByTestId("thingy-controls")).toBeInTheDocument();

    act(() => exitThingyRailFullScreen("other"));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes");
    act(() => exitThingyRailFullScreen("conv_1"));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("no");
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
  });

  it("remembers the session's selected tab", async () => {
    const { unmount } = renderWithThingies(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Report" }));
    unmount();

    renderWithThingies(<Rail conversationId="conv_1" />);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Report" })).toHaveAttribute("aria-selected", "true"),
    );
  });

  it("shows the next tab when the selected thingy goes, leaving full screen", async () => {
    const { queryClient } = renderWithThingies(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestThingyRailTab("conv_1", "thingy:1", { maximize: true }));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes");

    // Deleted, or no longer shared or shown here.
    act(() => queryClient.setQueryData(["thingies", "session", "conv_1"], [REPORT]));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report"),
    );
    expect(screen.getByLabelText("full screen")).toHaveTextContent("no");
  });

  it("keeps the tab order when a thingy is shown again", async () => {
    const { queryClient } = renderWithThingies(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });
    // The server lists a thingy shown again last.
    act(() => queryClient.setQueryData(["thingies", "session", "conv_1"], [REPORT, BOARD]));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Board", "Report"]);
  });

  it("opens the thingy the URL names full screen, and drops the name on leaving it", async () => {
    renderWithThingies(<Rail conversationId="conv_1" />, { path: "/c/conv_1?thingy=2" });
    await waitFor(() => expect(screen.getByLabelText("full screen")).toHaveTextContent("yes"));
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");

    act(() => exitThingyRailFullScreen("conv_1"));
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent(/^\/c\/conv_1$/),
    );
  });

  it("names the thingy in the URL while it fills the screen", async () => {
    renderWithThingies(<Rail conversationId="conv_1" />, { path: "/c/conv_1" });
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestThingyRailTab("conv_1", "thingy:1", { maximize: true }));
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_1?thingy=1"),
    );
    expect(useThingyRailRequest.getState().fullScreenThingyId).toBe("1");
  });

  it("covers the session with a requested thingy on a phone", async () => {
    renderWithThingies(<Rail conversationId="conv_1" mobile />);
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestThingyRailTab("conv_1", "thingy:1", { maximize: true }));
    const drawer = await screen.findByTestId("thingy-panel-drawer");
    await waitFor(() => expect(drawer).toHaveAttribute("data-state", "open"));
    expect(within(drawer).getByText("shows Board")).toBeInTheDocument();
    // Its controls sit in the drawer's header, beside Close.
    expect(within(drawer).getByRole("banner")).toContainElement(
      within(drawer).getByTestId("thingy-controls"),
    );

    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(drawer).toHaveAttribute("data-state", "closed");
  });

  it("offers no New thingy or close buttons to viewers who can't edit the session", async () => {
    renderWithThingies(<Rail conversationId="conv_1" readOnly />);
    await screen.findByRole("tab", { name: "Board" });
    expect(screen.getByRole("list", { name: "new items" })).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "Close Board" })).toBeNull();
  });

  it("shows nothing while the server has no thingies", () => {
    renderWithThingies(<Rail conversationId="conv_1" />, { info: SERVER_INFO_OFFLINE_FALLBACK });
    expect(screen.queryByRole("tab")).toBeNull();
    expect(getSessionThingies).not.toHaveBeenCalled();
  });
});

describe("New thingy tab", () => {
  it("builds the described thingy in this session and replaces itself with its tab", async () => {
    const created = thingyFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createThingy).mockResolvedValue(created);
    vi.mocked(linkThingyToSession).mockResolvedValue([BOARD, REPORT, created]);
    vi.mocked(openThingySession).mockResolvedValue("sent");
    renderWithThingies(<Rail conversationId="conv_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Thingy" }));
    expect(await screen.findByRole("tab", { name: "New thingy" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const content = screen.getByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the thingy" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));

    await waitFor(() => expect(openThingySession).toHaveBeenCalled());
    expect(createThingy).toHaveBeenCalledWith("An incident tracker for my team");
    expect(linkThingyToSession).toHaveBeenCalledWith("3", "conv_1", true);
    const [thingy, message, , options] = vi.mocked(openThingySession).mock.calls[0];
    expect(thingy).toBe(created);
    expect(message).toMatch(/An incident tracker for my team$/);
    expect(options).toMatchObject({ deliver: true, reveal: false, railSessionId: "conv_1" });
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Incident tracker" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.queryByRole("tab", { name: "New thingy" })).toBeNull();
  });

  it("closes without building on Cancel", async () => {
    renderWithThingies(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Thingy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("tab", { name: "New thingy" })).toBeNull();
    expect(createThingy).not.toHaveBeenCalled();
  });
});

describe("the thingy beside New session", () => {
  it("shows the thingy New session starts from, docked or full screen", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    renderWithThingies(
      <ThingyNewSessionPanel active sidebarOpen={false} onOpenSidebar={vi.fn()} />,
      {
        path: "/?thingy_start=1",
      },
    );

    const panel = await screen.findByRole("complementary", { name: "Board" });
    expect(panel).not.toHaveAttribute("data-maximized");
    expect(within(panel).getByText("shows Board")).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
    expect(panel).toHaveAttribute("data-maximized", "true");
    // The bar covers the header, so it offers the sidebar and moves the controls in.
    expect(within(panel).getByRole("button", { name: "Open sidebar" })).toBeInTheDocument();
    expect(
      within(screen.getByTestId("thingy-toolbar-slot")).getByTestId("thingy-controls"),
    ).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(panel).not.toHaveAttribute("data-maximized");
  });

  it("opens full screen when thingy code asks, and shows nothing without a thingy", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    useThingyRailRequest.setState({ newSessionMaximized: true });
    renderWithThingies(<ThingyNewSessionPanel active />, { path: "/?thingy_start=1" });
    expect(await screen.findByRole("complementary", { name: "Board" })).toHaveAttribute(
      "data-maximized",
      "true",
    );
  });

  it("stays away from other pages and from sessions", () => {
    const { unmount } = renderWithThingies(<ThingyNewSessionPanel active />, { path: "/" });
    expect(screen.queryByTestId("thingy-new-session-panel")).toBeNull();
    unmount();
    renderWithThingies(<ThingyNewSessionPanel active={false} />, { path: "/?thingy_start=1" });
    expect(screen.queryByTestId("thingy-new-session-panel")).toBeNull();
    expect(getThingy).not.toHaveBeenCalled();
  });

  it("names the thingy in the URL in full screen, and closes back to a plain New session", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    renderWithThingies(<ThingyNewSessionPanel active />, { path: "/?thingy_start=1" });
    const panel = await screen.findByRole("complementary", { name: "Board" });

    fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/?thingy_start=1&thingy=1");
    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(screen.getByLabelText("location")).toHaveTextContent(/^\/\?thingy_start=1$/);

    fireEvent.click(within(panel).getByRole("button", { name: "Close Board" }));
    expect(screen.getByLabelText("location")).toHaveTextContent(/^\/$/);
    expect(screen.queryByTestId("thingy-new-session-panel")).toBeNull();
  });

  it("closes the sidebar for full screen and opens it again after", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    const onSidebarOpenChange = vi.fn();
    renderWithThingies(
      <ThingyNewSessionPanel active sidebarOpen onSidebarOpenChange={onSidebarOpenChange} />,
      { path: "/?thingy_start=1" },
    );
    const panel = await screen.findByRole("complementary", { name: "Board" });
    fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
    expect(onSidebarOpenChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(onSidebarOpenChange).toHaveBeenLastCalledWith(true);
  });
});
