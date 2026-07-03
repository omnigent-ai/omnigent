// Tests for the admin MembersPage (invite, password reset, delete user).
//
// Browser e2e is impractical (admin/accounts-gated — would need a second
// authenticated server), so the surface is pinned here by mocking the
// mode-agnostic identity probe (resolveIdentity/getCurrentIsAdmin gate admin),
// accountsApi (listUsers/createInvite/resetUserPassword/deleteUser drive the
// table + actions), and useServerInfo (accounts_enabled toggles the
// manage-vs-read-only surface — the latter is the OIDC case).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MembersPage } from "./MembersPage";
import type { AccountListEntry } from "@/lib/accountsApi";
import * as accountsApi from "@/lib/accountsApi";
import * as identity from "@/lib/identity";

const mocks = vi.hoisted(() => ({ accountsEnabled: true }));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ accounts_enabled: mocks.accountsEnabled }),
}));
vi.mock("@/lib/identity", () => ({
  resolveIdentity: vi.fn(),
  getCurrentIsAdmin: vi.fn(),
}));
vi.mock("@/lib/accountsApi", () => ({
  listUsers: vi.fn(),
  createInvite: vi.fn(),
  resetUserPassword: vi.fn(),
  deleteUser: vi.fn(),
}));

function user(overrides: Partial<AccountListEntry> = {}): AccountListEntry {
  return {
    id: "bob",
    is_admin: false,
    created_at: null,
    last_login_at: null,
    has_password: true,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <MembersPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.accountsEnabled = true;
  vi.mocked(identity.resolveIdentity).mockResolvedValue("admin");
  vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
  vi.mocked(accountsApi.listUsers).mockResolvedValue([]);
  vi.mocked(accountsApi.createInvite).mockResolvedValue({
    ok: true,
    token: "tok",
    register_url: "https://app.example.com/register?invite=tok",
    expires_at: 9_999_999_999,
    is_admin: false,
  });
  vi.mocked(accountsApi.resetUserPassword).mockResolvedValue({
    ok: true,
    id: "bob",
    new_password: "fresh-pw-123",
  });
  vi.mocked(accountsApi.deleteUser).mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MembersPage gating", () => {
  it("shows a loading state until the identity probe resolves", () => {
    vi.mocked(identity.resolveIdentity).mockReturnValue(new Promise(() => {})); // never resolves
    renderPage();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("blocks non-admins with a permission message and never lists users", async () => {
    vi.mocked(identity.resolveIdentity).mockResolvedValue("alice");
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
    renderPage();
    expect(
      await screen.findByText("You don't have permission to manage members."),
    ).toBeInTheDocument();
    expect(accountsApi.listUsers).not.toHaveBeenCalled();
  });

  it("stays in the loading state for an unauthenticated visitor (resolveIdentity redirects)", async () => {
    // resolveIdentity returns null AND owns the login redirect, so the page
    // just never leaves its loading state — it must NOT list users.
    vi.mocked(identity.resolveIdentity).mockResolvedValue(null);
    renderPage();
    await waitFor(() => expect(identity.getCurrentIsAdmin).not.toHaveBeenCalled());
    expect(accountsApi.listUsers).not.toHaveBeenCalled();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });
});

describe("MembersPage table", () => {
  it("renders an empty state when there are no members", async () => {
    renderPage();
    expect(await screen.findByText("No members yet.")).toBeInTheDocument();
  });

  it("lists members with role badges and marks the current admin", async () => {
    vi.mocked(accountsApi.listUsers).mockResolvedValue([
      user({ id: "admin", is_admin: true }),
      user({ id: "bob" }),
    ]);
    renderPage();

    const adminRow = (await screen.findByText("admin")).closest("tr")!;
    expect(within(adminRow).getByText("Admin")).toBeInTheDocument();
    expect(within(adminRow).getByText("(you)")).toBeInTheDocument();

    const bobRow = screen.getByText("bob").closest("tr")!;
    expect(within(bobRow).getByText("Member")).toBeInTheDocument();
  });

  it("disables Remove for the current user and Reset for external (passwordless) users", async () => {
    vi.mocked(accountsApi.listUsers).mockResolvedValue([
      user({ id: "admin", is_admin: true }),
      user({ id: "ext", has_password: false }),
    ]);
    renderPage();

    const adminRow = (await screen.findByText("admin")).closest("tr")!;
    expect(within(adminRow).getByRole("button", { name: /Remove/ })).toBeDisabled();

    const extRow = screen.getByText("ext").closest("tr")!;
    expect(within(extRow).getByRole("button", { name: /Reset/ })).toBeDisabled();
  });
});

describe("MembersPage actions", () => {
  it("resets a user's password and shows the new password once", async () => {
    vi.mocked(accountsApi.listUsers).mockResolvedValue([user({ id: "bob" })]);
    renderPage();

    const bobRow = (await screen.findByText("bob")).closest("tr")!;
    fireEvent.click(within(bobRow).getByRole("button", { name: /Reset/ }));

    await waitFor(() => expect(accountsApi.resetUserPassword).toHaveBeenCalledWith("bob"));
    // The new password renders in a readonly, copyable input.
    expect(await screen.findByDisplayValue("fresh-pw-123")).toBeInTheDocument();
  });

  it("deletes a user through the confirmation dialog and refreshes the list", async () => {
    vi.mocked(accountsApi.listUsers)
      .mockResolvedValueOnce([user({ id: "bob" })])
      .mockResolvedValue([]); // after delete → refresh returns empty
    renderPage();

    const bobRow = (await screen.findByText("bob")).closest("tr")!;
    fireEvent.click(within(bobRow).getByRole("button", { name: /Remove/ }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Remove bob?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /^Remove$/ }));

    await waitFor(() => expect(accountsApi.deleteUser).toHaveBeenCalledWith("bob"));
    expect(await screen.findByText("No members yet.")).toBeInTheDocument();
  });

  it("creates an invite and surfaces the single-use URL", async () => {
    renderPage();
    await screen.findByText("No members yet.");

    fireEvent.click(screen.getByRole("button", { name: /Invite member/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Create invite/ }));

    await waitFor(() => expect(accountsApi.createInvite).toHaveBeenCalledWith(false));
    // The single-use invite URL renders in a readonly, copyable input.
    expect(await screen.findByDisplayValue(/register\?invite=tok/)).toBeInTheDocument();
  });
});

describe("MembersPage under OIDC (read-only)", () => {
  beforeEach(() => {
    // OIDC: accounts disabled → no password-based management. The list still
    // renders (admins can see who's provisioned), but every management
    // affordance is gone.
    mocks.accountsEnabled = false;
  });

  it("lists users but offers no management actions", async () => {
    vi.mocked(accountsApi.listUsers).mockResolvedValue([
      user({ id: "admin", is_admin: true }),
      user({ id: "bob" }),
    ]);
    renderPage();

    // The list renders with role badges.
    const bobRow = (await screen.findByText("bob")).closest("tr")!;
    expect(within(bobRow).getByText("Member")).toBeInTheDocument();

    // No invite button, no per-row Reset/Remove, and a read-only notice.
    expect(screen.queryByRole("button", { name: /Invite member/ })).toBeNull();
    expect(within(bobRow).queryByRole("button", { name: /Reset/ })).toBeNull();
    expect(within(bobRow).queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(screen.getByText(/provisioned automatically on first sign-in/)).toBeInTheDocument();
  });
});
