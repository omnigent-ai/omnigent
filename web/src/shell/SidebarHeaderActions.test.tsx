import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";

import { SidebarHeaderActions } from "./SidebarHeaderActions";

const mocks = vi.hoisted(() => ({ adminMode: null as boolean | null }));
vi.mock("@/lib/adminMode", () => ({ useAdminMode: () => mocks.adminMode }));

beforeEach(() => {
  mocks.adminMode = null;
});

afterEach(cleanup);

describe("SidebarHeaderActions shortcut hints", () => {
  it("marks the existing Settings button while admin mode is active", async () => {
    mocks.adminMode = true;
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );
    const settings = screen.getByLabelText("Settings");
    expect(settings).toHaveAttribute("href", "/settings");
    expect(settings).toContainElement(screen.getByLabelText("Admin mode on"));
    fireEvent.focus(settings);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Admin mode on");
  });
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
});
