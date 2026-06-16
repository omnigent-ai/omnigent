// Tests for AccountMenu's accounts-mode gating and dropdown surface.
//
// AccountMenu renders nothing unless (1) /v1/info reports accounts_enabled and
// (2) /auth/me resolves to an account. When both hold it shows the signed-in
// id, an "(admin)" marker + Members/Policies links for admins, and the
// Change-password / Sign-out actions. None of that had coverage: the component
// is mocked to null in every Sidebar test, so these pin the gating booleans and
// the menu items directly.
//
// useServerInfo and the accountsApi calls (getMe/changePassword/logout) are
// mocked so the component runs without a server; Link needs a router context.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountMenu } from "./AccountMenu";
import type { CurrentAccount } from "@/lib/accountsApi";
import type { ServerInfo } from "@/lib/capabilities";
import * as accountsApi from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));
vi.mock("@/lib/accountsApi", () => ({
  getMe: vi.fn(),
  changePassword: vi.fn(),
  logout: vi.fn(),
}));

const ACCOUNTS_ON: ServerInfo = {
  accounts_enabled: true,
  login_url: "/login",
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
};
const ACCOUNTS_OFF: ServerInfo = { ...ACCOUNTS_ON, accounts_enabled: false, login_url: null };

function account(overrides: Partial<CurrentAccount> = {}): CurrentAccount {
  return { id: "alice", is_admin: false, created_at: null, last_login_at: null, ...overrides };
}

function renderMenu() {
  return render(
    <MemoryRouter>
      <AccountMenu />
    </MemoryRouter>,
  );
}

/** Open the account dropdown. Radix DropdownMenu opens on pointerdown, not click. */
async function openMenu(triggerName: RegExp) {
  fireEvent.pointerDown(await screen.findByRole("button", { name: triggerName }), { button: 0 });
}

beforeEach(() => {
  vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_ON);
  vi.mocked(accountsApi.getMe).mockResolvedValue(account());
  vi.mocked(accountsApi.changePassword).mockResolvedValue({ ok: true });
  vi.mocked(accountsApi.logout).mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AccountMenu gating", () => {
  it("renders nothing when accounts mode is off (and never calls /auth/me)", () => {
    vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_OFF);
    const { container } = renderMenu();
    expect(container).toBeEmptyDOMElement();
    expect(accountsApi.getMe).not.toHaveBeenCalled();
  });

  it("renders nothing while the capabilities probe is still loading", () => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    const { container } = renderMenu();
    expect(container).toBeEmptyDOMElement();
    expect(accountsApi.getMe).not.toHaveBeenCalled();
  });

  it("renders nothing when /auth/me returns no account", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(null);
    const { container } = renderMenu();
    await waitFor(() => expect(accountsApi.getMe).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the signed-in account id once accounts is on and /auth/me resolves", async () => {
    renderMenu();
    expect(await screen.findByText("alice")).toBeInTheDocument();
  });
});

describe("AccountMenu dropdown surface", () => {
  it("hides admin-only links for a non-admin and shows Change password / Sign out", async () => {
    renderMenu();
    await openMenu(/alice/);

    expect(await screen.findByRole("menuitem", { name: /Change password/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Sign out/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Members/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Policies/ })).not.toBeInTheDocument();
  });

  it("shows Members + Policies links and the (admin) marker for an admin", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(account({ id: "root", is_admin: true }));
    renderMenu();
    await openMenu(/root/);

    expect(await screen.findByRole("menuitem", { name: /Members/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Policies/ })).toBeInTheDocument();
    expect(screen.getByText("(admin)")).toBeInTheDocument();
  });

  it("Sign out calls logout", async () => {
    renderMenu();
    await openMenu(/alice/);
    fireEvent.click(await screen.findByRole("menuitem", { name: /Sign out/ }));
    await waitFor(() => expect(accountsApi.logout).toHaveBeenCalledTimes(1));
  });

  it("Change password opens a dialog and submits the new password", async () => {
    renderMenu();
    await openMenu(/alice/);
    fireEvent.click(await screen.findByRole("menuitem", { name: /Change password/ }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(screen.getByPlaceholderText("Current password"), {
      target: { value: "oldpw" },
    });
    fireEvent.change(screen.getByPlaceholderText("New password"), {
      target: { value: "newpw-12345" },
    });
    fireEvent.change(screen.getByPlaceholderText("Confirm new password"), {
      target: { value: "newpw-12345" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    await waitFor(() =>
      expect(accountsApi.changePassword).toHaveBeenCalledWith({
        old_password: "oldpw",
        new_password: "newpw-12345",
      }),
    );
    expect(await screen.findByText("Your password has been changed.")).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
  });

  it("blocks submit and shows an error when the new passwords don't match", async () => {
    renderMenu();
    await openMenu(/alice/);
    fireEvent.click(await screen.findByRole("menuitem", { name: /Change password/ }));

    fireEvent.change(await screen.findByPlaceholderText("Current password"), {
      target: { value: "oldpw" },
    });
    fireEvent.change(screen.getByPlaceholderText("New password"), {
      target: { value: "newpw-12345" },
    });
    fireEvent.change(screen.getByPlaceholderText("Confirm new password"), {
      target: { value: "different" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("New passwords don't match.");
    expect(accountsApi.changePassword).not.toHaveBeenCalled();
  });
});
