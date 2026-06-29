// Tests for the Settings nav model + sidebar body (settingsNav).
//
// Covers the mobile-specific behavior: keyboard shortcuts is hidden on mobile
// (max-md:hidden), and "Back to Omnigent" does NOT close the sidebar overlay
// on a plain tap (no onNavClick) so mobile lands back on the conversation list
// instead of the homepage. Section links still close it.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const mocks = vi.hoisted(() => ({ accountsEnabled: false }));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ accounts_enabled: mocks.accountsEnabled }),
}));

import { SettingsSidebarBody, settingsNavGroups } from "./settingsNav";

function renderBody(opts: { onNavClick?: () => void; onClose?: () => void } = {}) {
  const onNavClick = opts.onNavClick ?? vi.fn();
  const onClose = opts.onClose ?? vi.fn();
  render(
    <TooltipProvider>
      <MemoryRouter initialEntries={["/settings/appearance"]}>
        <SettingsSidebarBody onNavClick={onNavClick} onClose={onClose} />
      </MemoryRouter>
    </TooltipProvider>,
  );
  return { onNavClick, onClose };
}

beforeEach(() => {
  mocks.accountsEnabled = false;
});
afterEach(cleanup);

describe("settingsNavGroups", () => {
  it("flags Keyboard shortcuts as hidden on mobile, but not the other items", () => {
    const items = settingsNavGroups(false, false).flatMap((g) => g.items);
    const shortcuts = items.find((i) => i.id === "shortcuts");
    expect(shortcuts?.hideOnMobile).toBe(true);
    for (const item of items) {
      if (item.id !== "shortcuts") expect(item.hideOnMobile).toBeFalsy();
    }
  });

  it("includes Account (leading) only when accounts auth is enabled", () => {
    expect(
      settingsNavGroups(false, false)
        .flatMap((g) => g.items)
        .map((i) => i.id),
    ).not.toContain("account");
    const withAccounts = settingsNavGroups(true, false)
      .flatMap((g) => g.items)
      .map((i) => i.id);
    expect(withAccounts).toContain("account");
    // Account leads its group — it's the most-visited section on accounts deploys.
    expect(withAccounts[0]).toBe("account");
  });

  it("includes the Local CLI section only in the desktop shell", () => {
    const ids = (isDesktop: boolean) =>
      settingsNavGroups(false, isDesktop)
        .flatMap((g) => g.items)
        .map((i) => i.id);
    expect(ids(false)).not.toContain("cli");
    expect(ids(true)).toContain("cli");
  });
});

describe("SettingsSidebarBody", () => {
  it("marks the Keyboard shortcuts nav item hidden on mobile via max-md:hidden", () => {
    renderBody();
    expect(screen.getByTestId("settings-nav-shortcuts").className).toContain("max-md:hidden");
    // Sibling items stay visible on every viewport.
    expect(screen.getByTestId("settings-nav-appearance").className).not.toContain("max-md:hidden");
    expect(screen.getByTestId("settings-nav-archived").className).not.toContain("max-md:hidden");
  });

  it("does NOT close the sidebar when 'Back to Omnigent' is tapped", () => {
    // No onNavClick on the back link: on mobile the overlay stays open so the
    // sidebar swaps back to the conversation list rather than closing onto the
    // homepage behind it.
    const { onNavClick } = renderBody();
    fireEvent.click(screen.getByRole("link", { name: /Back to Omnigent/ }));
    expect(onNavClick).not.toHaveBeenCalled();
  });

  it("DOES close the sidebar when a section is tapped (drills into content)", () => {
    const { onNavClick } = renderBody();
    fireEvent.click(screen.getByTestId("settings-nav-appearance"));
    expect(onNavClick).toHaveBeenCalledTimes(1);
  });
});
