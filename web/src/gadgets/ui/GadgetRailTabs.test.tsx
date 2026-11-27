import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import {
  exitGadgetRailFullScreen,
  noteGadgetFrameConnected,
  noteGadgetStartFailed,
  requestGadgetCreateSessionMenu,
  requestGadgetRailTab,
  resetGadgetRailRequests,
  setFullScreenGadget,
  useGadgetRailRequest,
  gadgetCoversConversation,
} from "@/lib/gadgetRailTabs";
import {
  type Gadget,
  createGadget,
  deleteGadget,
  getSessionGadgets,
  getGadget,
  getGadgetShelf,
  getGadgetGadgetSession,
  linkGadgetToSession,
} from "@/gadgets/api/gadgetApi";
import { openGadgetSession } from "@/gadgets/host/gadgetSession";
import {
  GadgetMobileDrawer,
  GadgetNewSessionPanel,
  GadgetRailTabStrip,
  resetGadgetTabOrders,
  useGadgetRailTabState,
} from "./GadgetRailTabs";
import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/sessionsApi";
import { gadgetKey } from "./gadgetQueries";
import { GADGETS_INFO, renderWithGadgets, gadgetFixture } from "./testUtils";
import { useLocation, useNavigate } from "@/lib/routing";
import { resetGadgetDrafts } from "./gadgetDrafts";
import { resetGadgetStarts } from "./useAdoptStartedGadgetSession";
import type * as GadgetSessionModule from "@/gadgets/host/gadgetSession";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getSessionGadgets: vi.fn(),
  linkGadgetToSession: vi.fn(),
  getGadget: vi.fn(),
  getGadgetShelf: vi.fn(),
  getGadgetGadgetSession: vi.fn(),
  createGadget: vi.fn(),
  deleteGadget: vi.fn(),
}));
vi.mock("@/gadgets/host/gadgetSession", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetSessionModule>()),
  openGadgetSession: vi.fn(),
}));
// The gadget itself (frame, comments) has its own tests; here it names itself and places its
// controls where the panel tells it.
vi.mock("./GadgetView", () => ({
  GadgetView: ({
    gadget,
    renderToolbar,
    toolbarTarget,
  }: {
    gadget: { name: string };
    renderToolbar: (commentsButton: ReactNode) => ReactNode;
    toolbarTarget: HTMLElement | null;
  }) => {
    const controls = <div data-testid="gadget-controls">{renderToolbar(null)}</div>;
    return (
      <>
        {toolbarTarget ? createPortal(controls, toolbarTarget) : controls}
        <p>shows {gadget.name}</p>
      </>
    );
  },
}));

const BOARD = gadgetFixture({ id: "1", name: "Board" });
const REPORT = gadgetFixture({ id: "2", name: "Report" });

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
  const state = useGadgetRailTabState(conversationId, {
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
      <GadgetRailTabStrip state={state} active />
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
        <GadgetMobileDrawer state={state} />
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
  vi.mocked(getSessionGadgets).mockResolvedValue([BOARD, REPORT]);
  vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD]);
  vi.mocked(getGadgetShelf).mockResolvedValue({ pinned: [], hasOwned: true });
});

afterEach(() => {
  resetGadgetRailRequests();
  resetGadgetDrafts();
  resetGadgetStarts();
  resetGadgetTabOrders();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("gadget tabs in a session's side panel", () => {
  it("lists the session's gadgets, shows the selected one, and closing it shows the tab next to it", async () => {
    const onShow = vi.fn();
    renderWithGadgets(<Rail conversationId="conv_1" onShow={onShow} />);

    fireEvent.click(await screen.findByRole("tab", { name: "Report" }));
    expect(onShow).toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "Report" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");

    fireEvent.click(screen.getByRole("button", { name: "Close Report" }));
    await waitFor(() => expect(linkGadgetToSession).toHaveBeenCalledWith("2", "conv_1", false));
    // The tab before it takes its place, not another kind of tab.
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
    // The server's answer replaces the session's list.
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Report" })).toBeNull());
  });

  it("takes a request for its session once the tab exists, and opens the panel", async () => {
    vi.mocked(getSessionGadgets).mockResolvedValue([]);
    const onReveal = vi.fn();
    const { queryClient } = renderWithGadgets(<Rail conversationId="conv_1" onReveal={onReveal} />);
    act(() => requestGadgetRailTab("other", "gadget:1"));
    act(() => requestGadgetRailTab("conv_1", "gadget:1"));
    await waitFor(() => expect(getSessionGadgets).toHaveBeenCalled());
    expect(onReveal).not.toHaveBeenCalled();

    act(() => queryClient.setQueryData(["gadgets", "session", "conv_1"], [BOARD]));
    await waitFor(() => expect(onReveal).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
  });

  it("switches to a requested tab again when the panel didn't stay on it, then lets the request go", async () => {
    let calls = 0;
    const onShow = vi.fn(() => {
      calls += 1;
      return calls > 1;
    });
    renderWithGadgets(<Rail conversationId="conv_1" onShow={onShow} />);
    await screen.findByRole("tab", { name: "Report" });

    vi.useFakeTimers();
    try {
      act(() => requestGadgetRailTab("conv_1", "gadget:2"));
      expect(onShow).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");
      // Kept a moment in case the panel is switched away again, then let go.
      expect(useGadgetRailRequest.getState().requested).not.toBeNull();
      act(() => vi.advanceTimersByTime(2000));
      expect(useGadgetRailRequest.getState().requested).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens a requested tab in full screen, keeping the sidebar when asked, and leaves it on request", async () => {
    renderWithGadgets(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });

    act(() => requestGadgetRailTab("conv_1", "gadget:1", { maximize: true, keepSidebar: true }));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes, with the sidebar");
    // In full screen the gadget's controls move into the panel's bar.
    expect(within(screen.getByLabelText("bar")).getByTestId("gadget-controls")).toBeInTheDocument();

    act(() => exitGadgetRailFullScreen("other"));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes");
    act(() => exitGadgetRailFullScreen("conv_1"));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("no");
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Board");
  });

  it("remembers the session's selected tab", async () => {
    const { unmount } = renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Report" }));
    unmount();

    renderWithGadgets(<Rail conversationId="conv_1" />);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Report" })).toHaveAttribute("aria-selected", "true"),
    );
  });

  it("shows the next tab when the selected gadget goes, leaving full screen", async () => {
    const { queryClient } = renderWithGadgets(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestGadgetRailTab("conv_1", "gadget:1", { maximize: true }));
    expect(screen.getByLabelText("full screen")).toHaveTextContent("yes");

    // Deleted, or no longer shared or shown here.
    act(() => queryClient.setQueryData(["gadgets", "session", "conv_1"], [REPORT]));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report"),
    );
    expect(screen.getByLabelText("full screen")).toHaveTextContent("no");
  });

  it("keeps the tab order when a gadget is shown again", async () => {
    const { queryClient } = renderWithGadgets(<Rail conversationId="conv_1" />);
    await screen.findByRole("tab", { name: "Board" });
    // The server lists a gadget shown again last.
    act(() => queryClient.setQueryData(["gadgets", "session", "conv_1"], [REPORT, BOARD]));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Board", "Report"]);
  });

  it("opens the gadget the URL names full screen, and drops the name on leaving it", async () => {
    renderWithGadgets(<Rail conversationId="conv_1" />, { path: "/c/conv_1?gadget=2" });
    await waitFor(() => expect(screen.getByLabelText("full screen")).toHaveTextContent("yes"));
    expect(screen.getByRole("region", { name: "content" })).toHaveTextContent("shows Report");

    act(() => exitGadgetRailFullScreen("conv_1"));
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent(/^\/c\/conv_1$/),
    );
  });

  it("names the gadget in the URL while it fills the screen", async () => {
    renderWithGadgets(<Rail conversationId="conv_1" />, { path: "/c/conv_1" });
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestGadgetRailTab("conv_1", "gadget:1", { maximize: true }));
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_1?gadget=1"),
    );
    expect(useGadgetRailRequest.getState().fullScreenGadgetId).toBe("1");
  });

  it("covers the session with a requested gadget on a phone", async () => {
    renderWithGadgets(<Rail conversationId="conv_1" mobile />);
    await screen.findByRole("tab", { name: "Board" });
    act(() => requestGadgetRailTab("conv_1", "gadget:1", { maximize: true }));
    const drawer = await screen.findByTestId("gadget-panel-drawer");
    await waitFor(() => expect(drawer).toHaveAttribute("data-state", "open"));
    expect(within(drawer).getByText("shows Board")).toBeInTheDocument();
    // Its controls sit in the drawer's header, beside Close.
    expect(within(drawer).getByRole("banner")).toContainElement(
      within(drawer).getByTestId("gadget-controls"),
    );

    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(drawer).toHaveAttribute("data-state", "closed");
  });

  it("offers viewers who can't edit the session no New gadget, but lets them close their own tabs", async () => {
    vi.mocked(linkGadgetToSession).mockResolvedValue([REPORT]);
    renderWithGadgets(<Rail conversationId="conv_1" readOnly />);
    await screen.findByRole("tab", { name: "Board" });
    expect(screen.getByRole("list", { name: "new items" })).toBeEmptyDOMElement();
    // The tab is on the viewer's own shelf, so closing it only hides it for them.
    fireEvent.click(screen.getByRole("button", { name: "Close Board" }));
    await waitFor(() =>
      expect(linkGadgetToSession).toHaveBeenCalledWith(BOARD.id, "conv_1", false),
    );
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Board" })).toBeNull());
  });

  it("shows nothing while the server has no gadgets", () => {
    renderWithGadgets(<Rail conversationId="conv_1" />, { info: SERVER_INFO_OFFLINE_FALLBACK });
    expect(screen.queryByRole("tab")).toBeNull();
    expect(getSessionGadgets).not.toHaveBeenCalled();
  });
});

describe("New gadget tab", () => {
  it("builds the described gadget in this session and replaces itself with its tab", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
    vi.mocked(openGadgetSession).mockResolvedValue("sent");
    renderWithGadgets(<Rail conversationId="conv_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    expect(await screen.findByRole("tab", { name: "New gadget" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const content = screen.getByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));

    await waitFor(() => expect(openGadgetSession).toHaveBeenCalled());
    expect(createGadget).toHaveBeenCalledWith("An incident tracker for my team");
    expect(linkGadgetToSession).toHaveBeenCalledWith("3", "conv_1", true);
    const [gadget, message, , options] = vi.mocked(openGadgetSession).mock.calls[0];
    expect(gadget).toBe(created);
    expect(message).toMatch(/An incident tracker for my team$/);
    expect(options).toMatchObject({ deliver: true, reveal: false, railSessionId: "conv_1" });
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Incident tracker" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.queryByRole("tab", { name: "New gadget" })).toBeNull();
  });

  it.each(["link", "send"] as const)(
    "tries a build whose %s failed again with the gadget it already made",
    async (failing) => {
      const created = gadgetFixture({
        id: "3",
        name: "Incident tracker",
        published_revision: null,
      });
      vi.mocked(createGadget).mockResolvedValue(created);
      vi.mocked(deleteGadget).mockResolvedValue(undefined);
      vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
      vi.mocked(openGadgetSession).mockResolvedValue("sent");
      if (failing === "link") {
        vi.mocked(linkGadgetToSession).mockRejectedValueOnce(new Error("Link failed"));
      } else {
        vi.mocked(openGadgetSession).mockRejectedValueOnce(new Error("Send failed"));
      }
      renderWithGadgets(<Rail conversationId="conv_1" />);

      fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
      const content = await screen.findByRole("region", { name: "content" });
      fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
        target: { value: "An incident tracker for my team" },
      });
      fireEvent.click(within(content).getByRole("button", { name: "Build" }));
      expect(
        await within(content).findByText(failing === "link" ? "Link failed" : "Send failed"),
      ).toBeVisible();

      fireEvent.click(within(content).getByRole("button", { name: "Build" }));
      await waitFor(() =>
        expect(screen.getByRole("tab", { name: "Incident tracker" })).toHaveAttribute(
          "aria-selected",
          "true",
        ),
      );
      // One gadget, finished on the second try; nothing left behind to delete.
      expect(createGadget).toHaveBeenCalledTimes(1);
      expect(deleteGadget).not.toHaveBeenCalled();
    },
  );

  it("deletes the gadget a failed build made when the draft is cancelled", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    vi.mocked(linkGadgetToSession).mockRejectedValueOnce(new Error("Link failed"));
    renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    const content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText("Link failed")).toBeVisible();
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("3"));
  });

  it.each([
    ["Cancel", "cancel"],
    ["a build with a changed description", "rebuild"],
  ])(
    "keeps a gadget whose build message may have reached the session on %s",
    async (_label, step) => {
      const created = gadgetFixture({
        id: "3",
        name: "Incident tracker",
        published_revision: null,
      });
      const next = gadgetFixture({ id: "4", name: "Release tracker", published_revision: null });
      vi.mocked(createGadget).mockResolvedValueOnce(created);
      vi.mocked(deleteGadget).mockResolvedValue(undefined);
      vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
      // Sent, but the answer was lost: the session may be building it.
      const { DeliveryFailed } = await vi.importActual<typeof GadgetSessionModule>(
        "@/gadgets/host/gadgetSession",
      );
      vi.mocked(openGadgetSession).mockRejectedValueOnce(
        new DeliveryFailed("UNAVAILABLE", "This session may not have got the request", false),
      );
      renderWithGadgets(<Rail conversationId="conv_1" />);
      fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
      const content = await screen.findByRole("region", { name: "content" });
      const box = within(content).getByRole("textbox", { name: "Describe the gadget" });
      fireEvent.change(box, { target: { value: "An incident tracker for my team" } });
      fireEvent.click(within(content).getByRole("button", { name: "Build" }));
      expect(await within(content).findByText(/may not have got the request/)).toBeVisible();
      if (step === "cancel") {
        fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
      } else {
        vi.mocked(createGadget).mockResolvedValueOnce(next);
        vi.mocked(openGadgetSession).mockResolvedValueOnce("sent");
        fireEvent.change(box, { target: { value: "A release tracker" } });
        fireEvent.click(within(content).getByRole("button", { name: "Build" }));
        await waitFor(() => expect(createGadget).toHaveBeenCalledTimes(2));
      }
      await act(async () => {});
      expect(deleteGadget).not.toHaveBeenCalledWith("3");
    },
  );

  it("still deletes a gadget whose build message the session refused", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
    const { DeliveryFailed } = await vi.importActual<typeof GadgetSessionModule>(
      "@/gadgets/host/gadgetSession",
    );
    vi.mocked(openGadgetSession).mockRejectedValueOnce(
      new DeliveryFailed("UNAVAILABLE", "This session did not get the request", true),
    );
    renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    const content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText(/did not get the request/)).toBeVisible();
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("3"));
  });

  it("keeps each session's draft and unfinished build its own", async () => {
    const madeInA = gadgetFixture({ id: "3", name: "Tracker A", published_revision: null });
    const madeInB = gadgetFixture({ id: "4", name: "Tracker B", published_revision: null });
    vi.mocked(createGadget).mockResolvedValueOnce(madeInA).mockResolvedValueOnce(madeInB);
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    vi.mocked(linkGadgetToSession).mockRejectedValue(new Error("Link failed"));
    // Both sessions have New gadget open, so switching keeps the same tab selected.
    const { rerender } = renderWithGadgets(<Rail conversationId="conv_b" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    await screen.findByRole("textbox", { name: "Describe the gadget" });
    rerender(<Rail conversationId="conv_a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    let content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "A tracker" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText("Link failed")).toBeVisible();

    rerender(<Rail conversationId="conv_b" />);
    content = await screen.findByRole("region", { name: "content" });
    const box = within(content).getByRole("textbox", { name: "Describe the gadget" });
    // B starts with its own empty draft, not A's text.
    expect(box).toHaveValue("");
    fireEvent.change(box, { target: { value: "A tracker" } });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText("Link failed")).toBeVisible();
    // B made its own gadget; A's unfinished one is neither reused nor deleted.
    expect(createGadget).toHaveBeenCalledTimes(2);
    expect(deleteGadget).not.toHaveBeenCalled();

    rerender(<Rail conversationId="conv_a" />);
    content = await screen.findByRole("region", { name: "content" });
    expect(within(content).getByRole("textbox", { name: "Describe the gadget" })).toHaveValue(
      "A tracker",
    );
    // Back in A, Try again reuses A's gadget rather than make a third.
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText("Link failed")).toBeVisible();
    expect(createGadget).toHaveBeenCalledTimes(2);
    expect(vi.mocked(linkGadgetToSession).mock.calls.at(-1)?.[0]).toBe(madeInA.id);
    // And Cancel there deletes A's gadget, leaving B's alone.
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith(madeInA.id));
    expect(deleteGadget).not.toHaveBeenCalledWith(madeInB.id);
  });

  it.each(["Cancel", "closing the tab"])(
    "deletes, without linking or sending, a gadget whose create finishes after %s",
    async (how) => {
      const created = gadgetFixture({
        id: "3",
        name: "Incident tracker",
        published_revision: null,
      });
      let finishCreate: (gadget: Gadget) => void = () => {};
      vi.mocked(createGadget).mockReturnValue(
        new Promise<Gadget>((resolve) => {
          finishCreate = resolve;
        }),
      );
      vi.mocked(deleteGadget).mockResolvedValue(undefined);
      renderWithGadgets(<Rail conversationId="conv_1" />);
      fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
      const content = await screen.findByRole("region", { name: "content" });
      fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
        target: { value: "An incident tracker for my team" },
      });
      fireEvent.click(within(content).getByRole("button", { name: "Build" }));
      await waitFor(() => expect(createGadget).toHaveBeenCalled());
      if (how === "Cancel")
        fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
      else fireEvent.click(screen.getByRole("button", { name: "Close New gadget" }));
      finishCreate(created);
      await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("3"));
      expect(linkGadgetToSession).not.toHaveBeenCalled();
      expect(openGadgetSession).not.toHaveBeenCalled();
      expect(screen.queryByRole("tab", { name: "Incident tracker" })).toBeNull();
    },
  );

  it("deletes a gadget whose create finishes after Cancel in a pane remounted meanwhile", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    let finishCreate: (gadget: Gadget) => void = () => {};
    vi.mocked(createGadget).mockReturnValue(
      new Promise<Gadget>((resolve) => {
        finishCreate = resolve;
      }),
    );
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    const { rerender } = renderWithGadgets(<Rail conversationId="conv_b" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    await screen.findByRole("textbox", { name: "Describe the gadget" });
    rerender(<Rail conversationId="conv_a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    let content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    await waitFor(() => expect(createGadget).toHaveBeenCalled());

    // Away and back: the pane is a new one, but Cancel still ends the build it started.
    rerender(<Rail conversationId="conv_b" />);
    await screen.findByRole("textbox", { name: "Describe the gadget" });
    rerender(<Rail conversationId="conv_a" />);
    content = await screen.findByRole("region", { name: "content" });
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
    finishCreate(created);

    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("3"));
    expect(linkGadgetToSession).not.toHaveBeenCalled();
    expect(openGadgetSession).not.toHaveBeenCalled();
  });

  it("deletes the gadget when its build message, on its way at Cancel, is refused", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
    const { DeliveryFailed } = await vi.importActual<typeof GadgetSessionModule>(
      "@/gadgets/host/gadgetSession",
    );
    let refuse: (error: unknown) => void = () => {};
    vi.mocked(openGadgetSession).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        refuse = reject;
      }),
    );
    renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    const content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    await waitFor(() => expect(openGadgetSession).toHaveBeenCalled());
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));
    // On its way: Cancel can't take it back yet.
    expect(deleteGadget).not.toHaveBeenCalled();

    refuse(new DeliveryFailed("UNAVAILABLE", "This session did not get the request", true));

    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("3"));
  });

  it("keeps a gadget whose build is still on its way after Cancel and a reopened form", async () => {
    const created = gadgetFixture({ id: "3", name: "Incident tracker", published_revision: null });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    vi.mocked(linkGadgetToSession).mockResolvedValue([BOARD, REPORT, created]);
    let deliver: (value: "sent") => void = () => {};
    vi.mocked(openGadgetSession).mockReturnValueOnce(
      new Promise((resolve) => {
        deliver = resolve;
      }),
    );
    renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    let content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "An incident tracker for my team" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    await waitFor(() => expect(openGadgetSession).toHaveBeenCalledTimes(1));
    fireEvent.click(within(content).getByRole("button", { name: "Cancel" }));

    // Reopened, and another build asked for while the first is still on its way.
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    content = await screen.findByRole("region", { name: "content" });
    fireEvent.change(within(content).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "A release dashboard" },
    });
    fireEvent.click(within(content).getByRole("button", { name: "Build" }));
    expect(await within(content).findByText(/still being sent/)).toBeVisible();
    deliver("sent");

    await waitFor(() => expect(openGadgetSession).toHaveBeenCalledTimes(1));
    // The session may be building the first gadget: it is kept, and sent only once.
    expect(deleteGadget).not.toHaveBeenCalled();
    expect(createGadget).toHaveBeenCalledTimes(1);
  });

  it("closes without building on Cancel", async () => {
    renderWithGadgets(<Rail conversationId="conv_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Gadget" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("tab", { name: "New gadget" })).toBeNull();
    expect(createGadget).not.toHaveBeenCalled();
  });
});

describe("the gadget beside New session", () => {
  it("shows the gadget New session starts from, docked or full screen", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    renderWithGadgets(
      <GadgetNewSessionPanel active sidebarOpen={false} onOpenSidebar={vi.fn()} />,
      {
        path: "/?gadget_start=1",
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
      within(screen.getByTestId("gadget-toolbar-slot")).getByTestId("gadget-controls"),
    ).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(panel).not.toHaveAttribute("data-maximized");
  });

  it("keeps a running gadget through a failed poll, and gives way once it is gone", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, retryDelay: 0 } },
    });
    renderWithGadgets(<GadgetNewSessionPanel active />, {
      path: "/?gadget_start=1",
      queryClient,
    });
    expect(await screen.findByText("shows Board")).toBeInTheDocument();

    vi.mocked(getGadget).mockRejectedValue(new ApiError("Service unavailable", 503, null));
    await act(async () => {
      await queryClient.refetchQueries();
      // The query notifies its observers after a tick.
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(queryClient.getQueryState(gadgetKey("1"))?.status).toBe("error");
    expect(screen.getByText("shows Board")).toBeInTheDocument();

    vi.mocked(getGadget).mockRejectedValue(new ApiError("Not found", 404, null));
    await act(async () => {
      await queryClient.refetchQueries();
    });
    await waitFor(() => expect(screen.queryByText("shows Board")).toBeNull());
  });

  it("opens full screen when gadget code asks, and shows nothing without a gadget", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    useGadgetRailRequest.setState({ newSessionMaximized: true });
    renderWithGadgets(<GadgetNewSessionPanel active />, { path: "/?gadget_start=1" });
    expect(await screen.findByRole("complementary", { name: "Board" })).toHaveAttribute(
      "data-maximized",
      "true",
    );
  });

  it("stays away from other pages and from sessions", () => {
    const { unmount } = renderWithGadgets(<GadgetNewSessionPanel active />, { path: "/" });
    expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
    unmount();
    renderWithGadgets(<GadgetNewSessionPanel active={false} />, { path: "/?gadget_start=1" });
    expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
    expect(getGadget).not.toHaveBeenCalled();
  });

  it.each([
    ["on", GADGETS_INFO],
    ["off", { ...GADGETS_INFO, features: {}, gadget_sharing: false }],
  ])("renders nothing for a malformed session URL with gadgets %s", (_state, info) => {
    for (const path of ["/c/%E0%A4", "/c/100%"]) {
      const { unmount } = renderWithGadgets(<GadgetNewSessionPanel active={false} />, {
        path,
        info,
      });
      expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
      unmount();
    }
  });

  it("names the gadget in the URL in full screen, and closes back to a plain New session", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    renderWithGadgets(<GadgetNewSessionPanel active />, { path: "/?gadget_start=1" });
    const panel = await screen.findByRole("complementary", { name: "Board" });

    fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/?gadget_start=1&gadget=1");
    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(screen.getByLabelText("location")).toHaveTextContent(/^\/\?gadget_start=1$/);

    fireEvent.click(within(panel).getByRole("button", { name: "Close Board" }));
    expect(screen.getByLabelText("location")).toHaveTextContent(/^\/$/);
    expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
  });

  it("keeps a full screen gadget full screen for a request before its session exists", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    const { openGadgetSession: openSession } = await vi.importActual<typeof GadgetSessionModule>(
      "@/gadgets/host/gadgetSession",
    );
    let navigate: ((url: string) => void) | null = null;
    function CaptureNavigate() {
      const go = useNavigate();
      useEffect(() => {
        navigate = (url: string) => go(url);
      }, [go]);
      return null;
    }
    renderWithGadgets(
      <>
        <GadgetNewSessionPanel active />
        <CaptureNavigate />
      </>,
      { path: "/?gadget_start=1&gadget=1" },
    );
    const panel = await screen.findByRole("complementary", { name: "Board" });
    await waitFor(() => expect(panel).toHaveAttribute("data-maximized", "true"));

    // A click in the full screen gadget before its session exists: New session's composer gets
    // the request, the gadget stays full screen, and the Create session menu opens beside it.
    await act(async () => {
      await openSession(BOARD, "Gadget action\n{}", navigate!, { deliver: true, actionId: "a1" });
    });

    expect(panel).toHaveAttribute("data-maximized", "true");
    const search = new URLSearchParams(
      screen.getByLabelText("location").textContent!.replace(/^[^?]*/, ""),
    );
    expect(search.get("prompt")).toContain("Gadget action");
    expect(search.get("gadget_start")).toBe("1");
    expect(search.get("gadget")).toBe("1");
    expect(await screen.findByRole("dialog", { name: "Create session" })).toBeInTheDocument();
  });

  describe("creating its session from the full screen gadget", () => {
    let navigate: ((url: string, replace?: boolean) => void) | null = null;
    function CaptureNavigate() {
      const go = useNavigate();
      useEffect(() => {
        navigate = (url: string, replace = false) => go(url, { replace });
      }, [go]);
      return null;
    }

    async function renderFullScreen(onSidebarOpenChange = vi.fn()) {
      vi.mocked(getGadget).mockResolvedValue(BOARD);
      // AppShell's New session panel: shown while no session is open.
      function Shell() {
        const location = useLocation();
        return (
          <GadgetNewSessionPanel
            active={!location.pathname.startsWith("/c/")}
            sidebarOpen
            onSidebarOpenChange={onSidebarOpenChange}
          />
        );
      }
      renderWithGadgets(
        <>
          <Shell />
          <CaptureNavigate />
        </>,
        { path: "/?gadget_start=1" },
      );
      const panel = await screen.findByRole("complementary", { name: "Board" });
      fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
      const gadget = within(panel).getByText("shows Board");
      // Create session: New session moves to the session at its temporary ID, then its real one.
      act(() => navigate!("/c/temp:abc?gadget_start=1"));
      return { panel, gadget, onSidebarOpenChange };
    }

    it("keeps the same gadget full screen over the session until the session shows it", async () => {
      const { panel, gadget, onSidebarOpenChange } = await renderFullScreen();

      // Not the session view: the same panel, and the same gadget in it, still fill the screen.
      expect(screen.getByRole("complementary", { name: "Board" })).toBe(panel);
      expect(panel).toHaveAttribute("data-maximized", "true");
      expect(panel).toHaveAttribute("aria-busy", "true");
      expect(panel).toHaveAttribute("inert");
      expect(within(panel).getByText("shows Board")).toBe(gadget);
      expect(within(panel).getByTestId("session-state-badge")).toHaveAttribute(
        "data-state",
        "starting",
      );
      act(() => navigate!("/c/conv_9", true));
      expect(within(panel).getByText("shows Board")).toBe(gadget);
      // On the session's own page before it links the gadget: neither the URL nor the full
      // screen state names it, yet its replies are hidden, so they stay unread.
      expect(gadgetCoversConversation(useGadgetRailRequest.getState(), "conv_9", null)).toBe(true);

      // The session shows the gadget full screen, but its frame isn't connected yet.
      act(() => setFullScreenGadget("1"));
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 300);
      });
      expect(screen.getByRole("complementary", { name: "Board" })).toBe(panel);

      act(() => noteGadgetFrameConnected("1"));
      await waitFor(() => expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull());
      expect(useGadgetRailRequest.getState().handoffCover).toBeNull();
      // The session's full screen keeps the sidebar as it is.
      expect(onSidebarOpenChange).not.toHaveBeenCalledWith(true);
    });

    it("shows the session when it couldn't take the gadget", async () => {
      const { onSidebarOpenChange } = await renderFullScreen();
      act(() => navigate!("/c/conv_9", true));

      act(() => noteGadgetStartFailed("1"));

      await waitFor(() => expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull());
      expect(onSidebarOpenChange).toHaveBeenLastCalledWith(true);
    });

    it("stops covering when the viewer goes elsewhere", async () => {
      await renderFullScreen();
      act(() => navigate!("/c/conv_9", true));
      expect(screen.getByTestId("gadget-new-session-panel")).toBeInTheDocument();

      act(() => navigate!("/c/conv_other"));

      expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
    });

    it("leaves the sidebar as it was through creating the session from an in-app open", async () => {
      vi.mocked(getGadget).mockResolvedValue(BOARD);
      const onSidebarOpenChange = vi.fn();
      function Shell() {
        const location = useLocation();
        return (
          <GadgetNewSessionPanel
            active={!location.pathname.startsWith("/c/")}
            sidebarOpen
            onSidebarOpenChange={onSidebarOpenChange}
          />
        );
      }
      renderWithGadgets(
        <>
          <Shell />
          <CaptureNavigate />
        </>,
        {
          path: { pathname: "/", search: "?gadget_start=1&gadget=1", state: { keepSidebar: true } },
        },
      );
      const panel = await screen.findByRole("complementary", { name: "Board" });
      await waitFor(() => expect(panel).toHaveAttribute("data-maximized", "true"));

      // Create session: the session's pages carry no navigation state.
      act(() => navigate!("/c/temp:abc?gadget_start=1"));
      act(() => navigate!("/c/conv_9", true));

      expect(screen.getByTestId("gadget-new-session-panel")).toBeInTheDocument();
      expect(onSidebarOpenChange).not.toHaveBeenCalled();
    });

    it("doesn't cover a session opened another way", async () => {
      vi.mocked(getGadget).mockResolvedValue(BOARD);
      useGadgetRailRequest.setState({ newSessionMaximized: true });
      function Shell() {
        const location = useLocation();
        return <GadgetNewSessionPanel active={!location.pathname.startsWith("/c/")} />;
      }
      renderWithGadgets(
        <>
          <Shell />
          <CaptureNavigate />
        </>,
        { path: "/?gadget_start=1&gadget=1" },
      );
      await screen.findByRole("complementary", { name: "Board" });

      act(() => navigate!("/c/conv_existing"));

      expect(screen.queryByTestId("gadget-new-session-panel")).toBeNull();
    });
  });

  it("shows a message the full screen gadget wrote in its Create session menu", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    useGadgetRailRequest.setState({ newSessionMaximized: true });
    renderWithGadgets(<GadgetNewSessionPanel active />, {
      path: "/?gadget_start=1&gadget=1&prompt=Rewrite%20every%20card",
    });
    const panel = await screen.findByRole("complementary", { name: "Board" });
    await waitFor(() => expect(panel).toHaveAttribute("data-maximized", "true"));

    act(() =>
      requestGadgetCreateSessionMenu({
        prompt: "Rewrite every card",
        gadgetId: "1",
        gadgetName: "Board",
      }),
    );

    const review = await screen.findByTestId("gadget-open-session-review");
    expect(review).toHaveTextContent("Written by the gadget “Board”");
    expect(review).toHaveTextContent("Rewrite every card");
  });

  it("closes the sidebar for full screen and opens it again after", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    const onSidebarOpenChange = vi.fn();
    renderWithGadgets(
      <GadgetNewSessionPanel active sidebarOpen onSidebarOpenChange={onSidebarOpenChange} />,
      { path: "/?gadget_start=1" },
    );
    const panel = await screen.findByRole("complementary", { name: "Board" });
    fireEvent.click(within(panel).getByRole("button", { name: "Full screen" }));
    expect(onSidebarOpenChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(within(panel).getByRole("button", { name: "Open session" }));
    expect(onSidebarOpenChange).toHaveBeenLastCalledWith(true);
  });
});
