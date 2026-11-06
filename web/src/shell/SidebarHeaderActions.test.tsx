import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";

import { SidebarHeaderActions } from "./SidebarHeaderActions";

const historyNavigation = vi.hoisted(() => ({
  availability: { canGoBack: true, canGoForward: true },
  request: vi.fn(),
}));
vi.mock("@/hooks/useHistoryNavigation", () => ({
  requestHistoryNavigation: (direction: -1 | 1) => historyNavigation.request(direction),
  useHistoryNavigationAvailability: () => historyNavigation.availability,
}));

afterEach(() => {
  cleanup();
  historyNavigation.request.mockReset();
  historyNavigation.availability = { canGoBack: true, canGoForward: true };
});

describe("SidebarHeaderActions shortcut hints", () => {
  it.each([
    {
      control: "Search",
      tooltip: "Search",
      keys: [MOD_KEY, "K"],
      aria: MOD_KEY === "⌘" ? "Meta+K" : "Control+K",
    },
    {
      control: "Settings",
      tooltip: "Settings",
      keys: [MOD_KEY, ALT_KEY, ","],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+," : "Control+Alt+,",
    },
    {
      control: "Close sidebar",
      tooltip: "Collapse sidebar",
      keys: [MOD_KEY, ALT_KEY, "["],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+[" : "Control+Alt+[",
    },
    {
      control: "Back",
      tooltip: "Back",
      keys: [MOD_KEY, "["],
      aria: MOD_KEY === "⌘" ? "Meta+[" : "Control+[",
    },
    {
      control: "Forward",
      tooltip: "Forward",
      keys: [MOD_KEY, "]"],
      aria: MOD_KEY === "⌘" ? "Meta+]" : "Control+]",
    },
  ])("shows the $control shortcut in its tooltip", async ({ control, tooltip, keys, aria }) => {
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const trigger = screen.getByLabelText(control);
    expect(trigger).toHaveAttribute("aria-keyshortcuts", aria);
    fireEvent.focus(trigger);
    const content = await screen.findByRole("tooltip");

    expect(content).toHaveTextContent(tooltip);
    expect(
      Array.from(content.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(keys);
  });

  it("leaves Settings to the native-shell profile footer when requested", () => {
    render(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} showSettings={false} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    expect(screen.queryByLabelText("Settings")).toBeNull();
    expect(screen.getByLabelText("Search")).toBeInTheDocument();
    expect(screen.getByLabelText("Close sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("history-navigation-buttons")).toHaveClass("max-md:hidden");
    expect(
      Array.from(
        screen.getByTestId("sidebar-header-actions").querySelectorAll<HTMLElement>("[aria-label]"),
        (control) => control.getAttribute("aria-label"),
      ),
    ).toEqual(["Close sidebar", "Search", "Back", "Forward"]);
  });

  it("hides Back and Forward until browser history has another entry", () => {
    historyNavigation.availability = { canGoBack: false, canGoForward: false };
    const view = render(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} showSettings={false} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("history-navigation-buttons")).toHaveClass("hidden");

    historyNavigation.availability = { canGoBack: true, canGoForward: false };
    view.rerender(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} showSettings={false} />
        </TooltipProvider>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("history-navigation-buttons")).not.toHaveClass("hidden");
  });

  it("disables each direction independently at the route-stack boundaries", () => {
    historyNavigation.availability = { canGoBack: true, canGoForward: false };
    const view = render(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("Back")).toBeEnabled();
    expect(screen.getByLabelText("Forward")).toBeDisabled();

    historyNavigation.availability = { canGoBack: false, canGoForward: true };
    view.rerender(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );
    expect(screen.getByLabelText("Back")).toBeDisabled();
    expect(screen.getByLabelText("Forward")).toBeEnabled();
  });

  it("maps the arrow buttons to browser history directions", () => {
    render(
      <MemoryRouter>
        <TooltipProvider>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByLabelText("Back"));
    fireEvent.click(screen.getByLabelText("Forward"));
    expect(historyNavigation.request.mock.calls).toEqual([[-1], [1]]);
  });
});
