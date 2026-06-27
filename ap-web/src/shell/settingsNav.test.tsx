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

// The Admin nav entry probes GET /v1/control-plane/me. Default to a
// "control plane absent" failure so the entry stays hidden in these tests
// (matching every non-Databricks-Apps deploy). A controllable mock lets the
// active-state test resolve it to admin so the entry renders.
const cpMe = vi.hoisted(() => vi.fn());
vi.mock("@/lib/controlPlaneApi", () => ({ getControlPlaneMe: cpMe }));

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
  cpMe.mockReset();
  cpMe.mockResolvedValue({ ok: false, error: "Not found.", status: 404 });
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

  it("includes the Admin entry (linking to /admin) only when showAdmin is true", () => {
    expect(
      settingsNavGroups(false, false)
        .flatMap((g) => g.items)
        .map((i) => i.id),
    ).not.toContain("admin");
    const admin = settingsNavGroups(false, true)
      .flatMap((g) => g.items)
      .find((i) => i.id === "admin");
    expect(admin?.to).toBe("/admin");
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

  it("marks the Admin nav entry active when on /admin (top-level route, not /settings/admin)", async () => {
    // Make the control-plane probe resolve to admin so the Admin entry renders.
    cpMe.mockResolvedValueOnce({
      ok: true,
      me: { user_id: "a@db.com", role: "admin", groups: [], is_platform_admin: false, capabilities: {} },
    });
    render(
      <TooltipProvider>
        <MemoryRouter initialEntries={["/admin"]}>
          <SettingsSidebarBody onNavClick={vi.fn()} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>,
    );
    // Entry appears after the async probe; it must be marked active on /admin
    // (the bug was that section-only matching never highlighted /admin).
    const admin = await screen.findByTestId("settings-nav-admin");
    expect(admin.getAttribute("aria-current")).toBe("page");
  });
});
