// Tests for the Settings nav model + sidebar body (settingsNav).
//
// Covers the mobile-specific behavior: keyboard shortcuts is hidden on mobile
// (max-md:hidden), and "Back to Omnigent" does NOT close the sidebar overlay
// on a plain tap (no onNavClick) so mobile lands back on the conversation list
// instead of the homepage. Section links still close it.

import { cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const mocks = vi.hoisted(() => ({
  accountsEnabled: false,
  // login_url: non-null for any sign-in mode (accounts OR OIDC), null in
  // header single-user. Gates the Account section + the bare-/settings default.
  loginUrl: null as string | null,
  isAdmin: false,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: mocks.accountsEnabled,
    login_url: mocks.loginUrl,
  }),
}));
// Admin gating is now mode-agnostic, sourced from `/v1/me` via useIsAdmin
// (not the accounts-only `/auth/me` useMe hook), so the Admin group appears
// under OIDC too.
vi.mock("@/hooks/useIsAdmin", () => ({
  useIsAdmin: () => mocks.isAdmin,
}));

import {
  SettingsSidebarBody,
  settingsNavGroups,
  useSettingsRoute,
  useTrackSettingsReturn,
} from "./settingsNav";

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
  mocks.loginUrl = null;
  mocks.isAdmin = false;
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

  it("includes Account (leading) whenever a login session exists (accounts OR OIDC)", () => {
    // First arg is hasAuthSession (login_url != null), not accounts-specific.
    // Header single-user (no session) → no Account section.
    expect(
      settingsNavGroups(false, false)
        .flatMap((g) => g.items)
        .map((i) => i.id),
    ).not.toContain("account");
    // Any login session → Account appears and leads its group.
    const withAccount = settingsNavGroups(true, false)
      .flatMap((g) => g.items)
      .map((i) => i.id);
    expect(withAccount).toContain("account");
    expect(withAccount[0]).toBe("account");
  });

  it("includes the Local CLI section only in the desktop shell", () => {
    const ids = (isDesktop: boolean) =>
      settingsNavGroups(false, isDesktop)
        .flatMap((g) => g.items)
        .map((i) => i.id);
    expect(ids(false)).not.toContain("cli");
    expect(ids(true)).toContain("cli");
  });

  it("includes the Admin group (Members / Policies) for any admin, in accounts OR OIDC mode", () => {
    const ids = (accountsEnabled: boolean, isAdmin: boolean) =>
      settingsNavGroups(accountsEnabled, false, isAdmin)
        .flatMap((g) => g.items)
        .map((i) => i.id);
    // Non-admin → no Members / Policies, regardless of auth mode.
    expect(ids(true, false)).not.toContain("members");
    expect(ids(false, false)).not.toContain("members");
    // Admin on an accounts deploy → both appear, grouped under "Admin".
    const accountsAdmin = settingsNavGroups(true, false, true).find((g) => g.title === "Admin");
    expect(accountsAdmin?.items.map((i) => i.id)).toEqual(["members", "policies"]);
    // Admin under OIDC (accountsEnabled false) → still appears. This is the
    // #1489 fix: OIDC previously had no admin chrome at all.
    const oidcAdmin = settingsNavGroups(false, false, true).find((g) => g.title === "Admin");
    expect(oidcAdmin?.items.map((i) => i.id)).toEqual(["members", "policies"]);
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

  it("'Back to Omnigent' returns to the conversation the user came from", () => {
    // Simulate the real flow: the sidebar (which stays mounted) tracks the
    // pre-settings location, then the user enters /settings. Back must point at
    // the conversation, not the home page.
    function Harness() {
      useTrackSettingsReturn();
      const navigate = useNavigate();
      const { inSettings } = useSettingsRoute();
      return (
        <>
          <button type="button" onClick={() => navigate("/settings")}>
            go-settings
          </button>
          {inSettings && <SettingsSidebarBody onNavClick={vi.fn()} onClose={vi.fn()} />}
        </>
      );
    }
    render(
      <TooltipProvider>
        <MemoryRouter initialEntries={["/c/conv_123?file=foo.ts"]}>
          <Harness />
        </MemoryRouter>
      </TooltipProvider>,
    );
    fireEvent.click(screen.getByText("go-settings"));
    expect(screen.getByRole("link", { name: /Back to Omnigent/ })).toHaveAttribute(
      "href",
      "/c/conv_123?file=foo.ts",
    );
  });

  it("DOES close the sidebar when a section is tapped (drills into content)", () => {
    const { onNavClick } = renderBody();
    fireEvent.click(screen.getByTestId("settings-nav-appearance"));
    expect(onNavClick).toHaveBeenCalledTimes(1);
  });

  it("renders Members / Policies sub-categories for an admin, linking under /settings", () => {
    mocks.accountsEnabled = true;
    mocks.isAdmin = true;
    renderBody();
    const members = screen.getByTestId("settings-nav-members");
    const policies = screen.getByTestId("settings-nav-policies");
    expect(members).toHaveAttribute("href", "/settings/members");
    expect(policies).toHaveAttribute("href", "/settings/policies");
  });

  it("renders the admin sub-categories for an admin under OIDC (accounts off)", () => {
    // #1489: admin chrome must surface under OIDC, where accounts is off.
    mocks.accountsEnabled = false;
    mocks.isAdmin = true;
    renderBody();
    expect(screen.getByTestId("settings-nav-members")).toHaveAttribute("href", "/settings/members");
    expect(screen.getByTestId("settings-nav-policies")).toHaveAttribute(
      "href",
      "/settings/policies",
    );
  });

  it("hides the admin sub-categories for a non-admin", () => {
    mocks.accountsEnabled = true;
    mocks.isAdmin = false;
    renderBody();
    expect(screen.queryByTestId("settings-nav-members")).toBeNull();
    expect(screen.queryByTestId("settings-nav-policies")).toBeNull();
  });
});

describe("useSettingsRoute", () => {
  function routeHook(path: string) {
    const w = ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
    );
    return renderHook(() => useSettingsRoute(), { wrapper: w }).result.current;
  }

  it("treats /settings/members and /settings/policies as in-settings sections on an accounts deploy", () => {
    // The core of the fix: Members / Policies now live UNDER /settings, so the
    // sidebar's `inSettings` gate stays true and the settings nav stays put —
    // the old standalone /members and /policies fell through to inSettings:false
    // (see the bare-path case below), which snapped the sidebar back to the
    // conversation list.
    mocks.accountsEnabled = true;
    expect(routeHook("/settings/members")).toEqual({ inSettings: true, section: "members" });
    expect(routeHook("/settings/policies")).toEqual({ inSettings: true, section: "policies" });
  });

  it("treats the admin sections as valid destinations even when accounts is off (OIDC)", () => {
    // #1489: Members / Policies are admin sections valid in ANY multi-user
    // mode (accounts AND OIDC). They no longer fall back to the default
    // section off an accounts deploy — the nav gates them on is_admin and the
    // pages self-gate / the server 403s.
    mocks.accountsEnabled = false;
    expect(routeHook("/settings/members")).toEqual({ inSettings: true, section: "members" });
    expect(routeHook("/settings/policies")).toEqual({ inSettings: true, section: "policies" });
  });

  it("reports NOT in settings for the legacy standalone /members and /policies paths", () => {
    // These paths only exist as redirects now; if one is ever hit directly it
    // must NOT read as in-settings (that was the bug's mechanism).
    expect(routeHook("/members").inSettings).toBe(false);
    expect(routeHook("/policies").inSettings).toBe(false);
  });

  it("keeps recognizing the other settings sections and their bare-path default", () => {
    expect(routeHook("/settings/appearance")).toEqual({
      inSettings: true,
      section: "appearance",
    });
    // Bare /settings: in-settings, defaulting to Appearance in header mode
    // (no login session — loginUrl null per beforeEach).
    expect(routeHook("/settings")).toEqual({ inSettings: true, section: "appearance" });
    // A non-settings route is out of settings.
    expect(routeHook("/inbox").inSettings).toBe(false);
  });

  it("defaults bare /settings to Account when a login session exists", () => {
    // login_url set (accounts OR OIDC) → Account is the landing section.
    mocks.loginUrl = "/login";
    expect(routeHook("/settings")).toEqual({ inSettings: true, section: "account" });
  });

  it("matches the settings segment under an embed basename", () => {
    // Basename-agnostic: the sidebar rebases links behind the app's back in the
    // embed, so detection keys off the `settings` segment wherever it lands.
    mocks.accountsEnabled = true;
    expect(routeHook("/ml/omnigent-embed/settings/members")).toEqual({
      inSettings: true,
      section: "members",
    });
  });
});
