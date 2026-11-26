import { type ReactElement, type ReactNode, useState } from "react";
import { act, fireEvent, render as renderUi, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { markConversationUnread } from "@/hooks/useUnseenConversations";

import {
  requestGadgetCreateSessionMenu,
  resetGadgetRailRequests,
  useGadgetRailRequest,
} from "@/lib/gadgetRailTabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  MobileHeaderControlPortal,
  MobileHeaderControlsProvider,
  SessionControlPortal,
} from "@/shell/MobileHeaderControlsSlot";
import { GadgetOpenSessionButton } from "./GadgetOpenSessionButton";

let queryClient = new QueryClient();

function render(ui: ReactElement) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
  return renderUi(ui, { wrapper });
}

/** Puts the session's row in a sidebar list, where its status comes from. */
function listSession(row: Partial<Conversation> & { id: string }) {
  queryClient.setQueryData(["conversations", "test"], {
    pages: [
      {
        data: [
          {
            object: "conversation",
            title: "Session",
            created_at: 1000,
            updated_at: 2000,
            labels: {},
            permission_level: null,
            owner: "viewer@example.com",
            status: "idle",
            ...row,
          },
        ],
      },
    ],
    pageParams: [null],
  });
}

// A composer: its host, permission and harness controls keep their own state, like New
// session's draft, and render through the session control portals the real composers use.
function Composer({
  sessionId,
  harness,
  createError,
}: {
  sessionId: string | null;
  harness: string;
  createError?: string;
}) {
  const [mode, setMode] = useState("Ask");
  const [created, setCreated] = useState(false);
  return (
    <div data-testid="composer">
      <MobileHeaderControlPortal sessionId={sessionId} popoverSlot="host">
        <span>Host: laptop</span>
      </MobileHeaderControlPortal>
      <SessionControlPortal sessionId={sessionId} slot="permission">
        <button type="button" onClick={() => setMode("Auto")}>
          Permission: {mode}
        </button>
      </SessionControlPortal>
      <MobileHeaderControlPortal sessionId={sessionId} popoverSlot="harness">
        <span>Model: {harness}-model</span>
      </MobileHeaderControlPortal>
      <SessionControlPortal sessionId={sessionId} slot="icon" inPlace={false}>
        <svg data-testid={`${harness}-icon`} />
      </SessionControlPortal>
      {/* New session's Create session button, with its error, as the landing renders them. */}
      <SessionControlPortal sessionId={sessionId} slot="create" inPlace={false}>
        {createError && <p role="alert">{createError}</p>}
        <button type="button" onClick={() => setCreated(true)}>
          Create session
        </button>
      </SessionControlPortal>
      {created && <span>Session created as {mode}</span>}
    </div>
  );
}

function renderButton({
  sessionId,
  composerSessionId = sessionId,
  harness = "codex",
  onOpenSession = vi.fn(),
  createError,
  starting,
  gadgetId = null,
}: {
  gadgetId?: string | null;
  sessionId: string | null;
  composerSessionId?: string | null;
  harness?: string;
  onOpenSession?: () => void;
  createError?: string;
  starting?: boolean;
}) {
  render(
    <MobileHeaderControlsProvider>
      <GadgetOpenSessionButton
        sessionId={sessionId}
        gadgetId={gadgetId}
        starting={starting}
        onOpenSession={onOpenSession}
      />
      <Composer sessionId={composerSessionId} harness={harness} createError={createError} />
    </MobileHeaderControlsProvider>,
  );
  return { onOpenSession, button: screen.getByRole("button", { name: "Open session" }) };
}

function hoverOpen(button: HTMLElement) {
  fireEvent.pointerOver(button);
  act(() => {
    vi.advanceTimersByTime(400);
  });
}

describe("GadgetOpenSessionButton", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    queryClient = new QueryClient();
  });
  afterEach(() => {
    vi.useRealTimers();
    resetGadgetRailRequests();
  });

  it("shows the session's harness icon in color", () => {
    renderButton({ sessionId: "conv_1", harness: "claude" });

    const icon = screen.getByTestId("gadget-open-session-icon");
    expect(within(icon).getByTestId("claude-icon")).toBeInTheDocument();
    expect(icon).not.toHaveClass("grayscale");
  });

  it("shows New session's harness in grey before the session exists", () => {
    renderButton({ sessionId: null, harness: "codex" });

    const icon = screen.getByTestId("gadget-open-session-icon");
    expect(within(icon).getByTestId("codex-icon")).toBeInTheDocument();
    expect(icon).toHaveClass("grayscale");
  });

  it("keeps another session's controls out of the button", () => {
    renderButton({ sessionId: "conv_1", composerSessionId: "conv_2" });

    const icon = screen.getByTestId("gadget-open-session-icon");
    expect(icon).toBeEmptyDOMElement();
    expect(screen.getByTestId("composer")).toHaveTextContent("Permission: Ask");
  });

  it("opens the composer's controls in a popover on hover", () => {
    const { button } = renderButton({ sessionId: "conv_1" });
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();

    hoverOpen(button);

    const popover = screen.getByTestId("gadget-open-session-popover");
    expect(within(popover).getByText("Host: laptop")).toBeInTheDocument();
    expect(within(popover).getByRole("button", { name: "Permission: Ask" })).toBeInTheDocument();
    expect(within(popover).getByText("Model: codex-model")).toBeInTheDocument();
    // The controls moved; the composer no longer shows them.
    expect(screen.getByTestId("composer")).not.toHaveTextContent("Host: laptop");
  });

  it("doesn't open for a pointer that only passes over the button", () => {
    const { button } = renderButton({ sessionId: "conv_1" });

    fireEvent.pointerOver(button);
    act(() => {
      vi.advanceTimersByTime(100);
    });
    fireEvent.pointerLeave(button);
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
  });

  it("applies a choice made in the popover to the composer's own draft", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    fireEvent.click(screen.getByRole("button", { name: "Permission: Ask" }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
    expect(screen.getByTestId("composer")).toHaveTextContent("Permission: Auto");
  });

  it("stays open while its controls are in use, even when the pointer leaves", () => {
    const { button } = renderButton({ sessionId: "conv_1" });
    hoverOpen(button);
    const popover = screen.getByTestId("gadget-open-session-popover");

    fireEvent.pointerDown(within(popover).getByRole("button", { name: "Permission: Ask" }));
    fireEvent.pointerLeave(popover);
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();
  });

  it("leaves full screen when the icon is clicked", () => {
    const { button, onOpenSession } = renderButton({ sessionId: "conv_1" });

    fireEvent.pointerDown(button);
    fireEvent.focus(button);
    fireEvent.click(button);

    expect(onOpenSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
  });

  it("leaves full screen from the popover's Open session button", () => {
    const { button, onOpenSession } = renderButton({ sessionId: "conv_1" });
    hoverOpen(button);

    fireEvent.click(screen.getByTestId("gadget-open-session-button"));

    expect(onOpenSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
    expect(screen.getByTestId("composer")).toHaveTextContent("Host: laptop");
  });

  it("opens on keyboard focus and closes on Escape", () => {
    const { button } = renderButton({ sessionId: "conv_1" });

    fireEvent.focus(button);
    expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();

    fireEvent.keyDown(button, { key: "Escape" });
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
  });

  it("offers New session's Create session instead of Open session before the session exists", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    const popover = screen.getByRole("dialog", { name: "Create session" });
    expect(within(popover).getByRole("button", { name: "Create session" })).toBeInTheDocument();
    expect(within(popover).getByTestId("gadget-open-session-create")).not.toBeEmptyDOMElement();
  });

  it("opens the Create session menu, focused, when the gadget asks before its session exists", () => {
    renderButton({ sessionId: null });

    act(() => {
      requestGadgetCreateSessionMenu();
    });
    act(() => {
      vi.advanceTimersByTime(50);
    });

    const popover = screen.getByRole("dialog", { name: "Create session" });
    expect(document.activeElement).toBe(
      within(popover).getByRole("button", { name: "Create session" }),
    );
    // The request is taken, so the menu doesn't open again on its own.
    expect(useGadgetRailRequest.getState().createSessionMenuRequested).toBe(false);

    // Opened for a request, it stays while the pointer moves away, and Escape closes it.
    fireEvent.pointerLeave(popover);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
  });

  it("shows a message the gadget wrote, and where it came from, before Create session sends it", () => {
    renderButton({ sessionId: null, gadgetId: "g1" });
    act(() => {
      requestGadgetCreateSessionMenu({
        prompt: "Delete every branch\nthen push",
        gadgetId: "g1",
        gadgetName: "Synthetic board",
      });
    });
    act(() => {
      vi.advanceTimersByTime(50);
    });

    const popover = screen.getByRole("dialog", { name: "Create session" });
    const review = within(popover).getByTestId("gadget-open-session-review");
    expect(review).toHaveTextContent("Written by the gadget “Synthetic board”");
    expect(review).toHaveTextContent("Delete every branch then push");
    // Focus is on the menu, so Enter doesn't send the message unread.
    expect(document.activeElement).toBe(popover);
    expect(document.activeElement).not.toBe(
      within(popover).getByRole("button", { name: "Create session" }),
    );
  });

  it("doesn't show another gadget's message", () => {
    renderButton({ sessionId: null, gadgetId: "g2" });
    act(() => {
      requestGadgetCreateSessionMenu({ prompt: "Synthetic", gadgetId: "g1", gadgetName: "One" });
    });
    act(() => {
      vi.advanceTimersByTime(50);
    });
    expect(screen.queryByTestId("gadget-open-session-review")).toBeNull();
  });

  it("creates the session with the choices made in the menu, without leaving full screen", () => {
    const { onOpenSession } = renderButton({ sessionId: null });
    act(() => {
      requestGadgetCreateSessionMenu();
    });

    fireEvent.click(screen.getByRole("button", { name: "Permission: Ask" }));
    fireEvent.click(screen.getByRole("button", { name: "Create session" }));

    expect(screen.getByText("Session created as Auto")).toBeInTheDocument();
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it("shows why creating the session failed in the menu", () => {
    renderButton({ sessionId: null, createError: "No host is connected" });
    act(() => {
      requestGadgetCreateSessionMenu();
    });

    expect(
      within(screen.getByTestId("gadget-open-session-popover")).getByRole("alert"),
    ).toHaveTextContent("No host is connected");
  });

  it("leaves a request for the Create session menu to New session's button", () => {
    renderButton({ sessionId: "conv_1" });

    act(() => {
      requestGadgetCreateSessionMenu();
    });

    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
    expect(useGadgetRailRequest.getState().createSessionMenuRequested).toBe(true);
  });

  it("dims the screen behind a menu opened for a request, and a press there dismisses it", () => {
    const { onOpenSession } = renderButton({ sessionId: null });
    act(() => {
      requestGadgetCreateSessionMenu();
    });

    const popover = screen.getByRole("dialog", { name: "Create session" });
    expect(popover).toHaveAttribute("aria-modal", "true");
    const scrim = screen.getByTestId("gadget-open-session-scrim");
    // Under the menu, over everything else, the gadget included.
    expect(scrim).toHaveClass("fixed", "inset-0", "z-[49]");
    expect(scrim.parentElement).toBe(document.body);
    expect(scrim).toBeEmptyDOMElement();
    // Marks the root, which index.css uses to drop the sidebar under the dimming.
    expect(document.documentElement).toHaveAttribute("data-gadget-session-scrim");
    // The menu's own controls stay usable over it.
    fireEvent.click(within(popover).getByRole("button", { name: "Permission: Ask" }));
    expect(screen.getByTestId("gadget-open-session-scrim")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(10);
    });
    fireEvent.pointerDown(scrim, { button: 0 });
    fireEvent.click(scrim);

    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
    expect(screen.queryByTestId("gadget-open-session-scrim")).toBeNull();
    expect(document.documentElement).not.toHaveAttribute("data-gadget-session-scrim");
    // Dismissing creates nothing and keeps New session's draft for later.
    expect(onOpenSession).not.toHaveBeenCalled();
    expect(screen.getByTestId("composer")).toHaveTextContent("Permission: Auto");
  });

  it("dismisses the dimmed menu on Escape", () => {
    renderButton({ sessionId: null });
    act(() => {
      requestGadgetCreateSessionMenu();
    });
    expect(screen.getByTestId("gadget-open-session-scrim")).toBeInTheDocument();

    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
    expect(screen.queryByTestId("gadget-open-session-scrim")).toBeNull();
  });

  it("focuses the menu itself when Create session can't be pressed yet", () => {
    render(
      <MobileHeaderControlsProvider>
        <GadgetOpenSessionButton sessionId={null} onOpenSession={vi.fn()} />
        <MobileHeaderControlPortal sessionId={null} popoverSlot="host">
          <button type="button">Host: none</button>
        </MobileHeaderControlPortal>
        <SessionControlPortal sessionId={null} slot="create" inPlace={false}>
          <button type="button" disabled>
            Create session
          </button>
        </SessionControlPortal>
      </MobileHeaderControlsProvider>,
    );
    act(() => {
      requestGadgetCreateSessionMenu();
    });
    act(() => {
      vi.advanceTimersByTime(50);
    });

    const popover = screen.getByTestId("gadget-open-session-popover");
    expect(document.activeElement).toBe(popover);
    fireEvent.keyDown(popover, { key: "Escape" });
    expect(screen.queryByTestId("gadget-open-session-popover")).toBeNull();
  });

  it("doesn't dim the screen for a menu opened by hover", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    expect(screen.getByTestId("gadget-open-session-popover")).not.toHaveAttribute("aria-modal");
    expect(screen.queryByTestId("gadget-open-session-scrim")).toBeNull();
  });

  it("orders host, permission and harness, with the main button after them", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    const controls = screen.getByTestId("gadget-open-session-controls");
    expect(Array.from(controls.children).map((slot) => slot.getAttribute("data-testid"))).toEqual([
      "gadget-open-session-host",
      "gadget-open-session-permission",
      "gadget-open-session-harness",
    ]);
    const create = screen.getByTestId("gadget-open-session-create");
    expect(
      controls.compareDocumentPosition(create) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(create).getByRole("button", { name: "Create session" })).toBeInTheDocument();
  });

  describe("the session's status", () => {
    const badge = () => screen.queryByTestId("session-state-badge");

    it("shows a session waiting for the viewer", () => {
      listSession({ id: "conv_wait", status: "running", pending_elicitations_count: 1 });
      renderButton({ sessionId: "conv_wait" });
      expect(badge()).toHaveAttribute("data-state", "awaiting");
      expect(badge()).toHaveTextContent("Needs response");
    });

    it("shows a running session", () => {
      listSession({ id: "conv_run", status: "running" });
      renderButton({ sessionId: "conv_run" });
      expect(badge()).toHaveAttribute("data-state", "running");
    });

    it("shows new messages the viewer hasn't read", () => {
      listSession({ id: "conv_new", status: "idle", updated_at: 2000 });
      markConversationUnread("conv_new", 2000);
      renderButton({ sessionId: "conv_new" });
      expect(badge()).toHaveAttribute("data-state", "unseen");
    });

    it("shows nothing for a quiet session, or before one exists", () => {
      listSession({ id: "conv_quiet", status: "idle" });
      renderButton({ sessionId: "conv_quiet" });
      expect(badge()).toBeNull();
    });

    it("shows a session New session just created as starting", () => {
      renderButton({ sessionId: null, starting: true });
      expect(badge()).toHaveAttribute("data-state", "starting");
    });
  });

  it("hides rows for controls the composer doesn't offer", () => {
    render(
      <MobileHeaderControlsProvider>
        <GadgetOpenSessionButton sessionId="conv_1" onOpenSession={() => undefined} />
        <MobileHeaderControlPortal sessionId="conv_1" popoverSlot="harness">
          <span>Model: only</span>
        </MobileHeaderControlPortal>
      </MobileHeaderControlsProvider>,
    );
    hoverOpen(screen.getByRole("button", { name: "Open session" }));

    expect(screen.getByTestId("gadget-open-session-host")).toBeEmptyDOMElement();
    expect(screen.getByTestId("gadget-open-session-harness")).toHaveTextContent("Model: only");
    // No icon was offered, so the button shows the chat icon.
    expect(screen.getByTestId("gadget-open-session-icon")).toBeEmptyDOMElement();
  });

  describe("with a control that opens its own menu", () => {
    // The real host picker: a dropdown whose menu the composer renders in a portal of its own,
    // outside the Open session menu.
    function HostPicker() {
      const [host, setHost] = useState("laptop");
      return (
        <MobileHeaderControlPortal sessionId={null} popoverSlot="host">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button">Host: {host}</button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem onSelect={() => setHost("devbox")}>devbox</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </MobileHeaderControlPortal>
      );
    }

    function renderWithPicker(onOpenSession = vi.fn()) {
      render(
        <MobileHeaderControlsProvider>
          <GadgetOpenSessionButton sessionId={null} onOpenSession={onOpenSession} />
          <HostPicker />
          <iframe title="gadget" sandbox="allow-scripts" />
        </MobileHeaderControlsProvider>,
      );
      const button = screen.getByRole("button", { name: "Open session" });
      hoverOpen(button);
      return { button, onOpenSession };
    }

    function openHostMenu() {
      const trigger = screen.getByRole("button", { name: "Host: laptop" });
      act(() => {
        trigger.focus();
      });
      fireEvent.keyDown(trigger, { key: "Enter" });
      return screen.getByRole("menu");
    }

    it("stays open while the control's menu is used, and keeps the choice", () => {
      const { onOpenSession } = renderWithPicker();

      const menu = openHostMenu();
      // Focus moved into the control's menu, outside the Open session menu.
      expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();
      const item = within(menu).getByRole("menuitem", { name: "devbox" });
      fireEvent.pointerDown(item);
      fireEvent.keyDown(item, { key: "Enter" });
      act(() => {
        vi.advanceTimersByTime(1000);
      });

      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      const popover = screen.getByTestId("gadget-open-session-popover");
      expect(within(popover).getByRole("button", { name: "Host: devbox" })).toBeInTheDocument();
      expect(onOpenSession).not.toHaveBeenCalled();
    });

    it("stays open when the pointer moves onto a control", () => {
      const { button } = renderWithPicker();
      const popover = screen.getByTestId("gadget-open-session-popover");

      fireEvent.pointerLeave(button);
      // The control is the composer's, but in the menu's DOM: the pointer is still in the menu.
      fireEvent.pointerEnter(popover);
      act(() => {
        vi.advanceTimersByTime(1000);
      });

      expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();
    });

    it("closes a control's menu on Escape before closing itself", () => {
      renderWithPicker();
      const menu = openHostMenu();

      fireEvent.keyDown(menu, { key: "Escape" });
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();

      fireEvent.keyDown(screen.getByTestId("gadget-open-session-popover"), { key: "Escape" });
      expect(screen.queryByTestId("gadget-open-session-popover")).not.toBeInTheDocument();
    });

    it("closes on a press outside it, after a control's menu was used", async () => {
      vi.useRealTimers();
      render(
        <MobileHeaderControlsProvider>
          <GadgetOpenSessionButton sessionId={null} onOpenSession={vi.fn()} />
          <HostPicker />
        </MobileHeaderControlsProvider>,
      );
      // Keyboard focus opens the menu at once.
      act(() => {
        screen.getByRole("button", { name: "Open session" }).focus();
      });
      openHostMenu();
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
      expect(screen.getByTestId("gadget-open-session-popover")).toBeInTheDocument();
      // Radix listens for presses outside a tick after it opens.
      await act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 0);
          }),
      );

      fireEvent.pointerDown(document.body, { button: 0 });
      fireEvent.click(document.body);

      expect(screen.queryByTestId("gadget-open-session-popover")).not.toBeInTheDocument();
    });

    it("closes when the viewer clicks into the gadget", () => {
      renderWithPicker();

      act(() => {
        screen.getByTitle("gadget").focus();
        window.dispatchEvent(new FocusEvent("blur"));
      });

      expect(screen.queryByTestId("gadget-open-session-popover")).not.toBeInTheDocument();
    });
  });
});
