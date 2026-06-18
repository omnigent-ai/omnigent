// Tests for the admin MembersPage (invite, password reset, delete user).
//
// Browser e2e is impractical (admin/accounts-gated — would need a second
// authenticated server), so the surface is pinned here by mocking accountsApi
// (getMe gates admin; listUsers/createInvite/resetUserPassword/deleteUser drive
// the table + actions) and useNavigate (to observe the unauth → /login bounce).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MembersPage } from "./MembersPage";
import type { AccountListEntry } from "@/lib/accountsApi";
import * as accountsApi from "@/lib/accountsApi";

const navigateMock = vi.fn();

vi.mock("@/lib/routing", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/routing")>()),
  useNavigate: () => navigateMock,
}));
vi.mock("@/lib/accountsApi", () => ({
  getMe: vi.fn(),
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
  vi.mocked(accountsApi.getMe).mockResolvedValue({
    id: "admin",
    is_admin: true,
    created_at: null,
    last_login_at: null,
  });
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
    vi.mocked(accountsApi.getMe).mockReturnValue(new Promise(() => {})); // never resolves
    renderPage();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("blocks non-admins with a permission message and never lists users", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue({
      id: "alice",
      is_admin: false,
      created_at: null,
      last_login_at: null,
    });
    renderPage();
    expect(
      await screen.findByText("You don't have permission to manage members."),
    ).toBeInTheDocument();
    expect(accountsApi.listUsers).not.toHaveBeenCalled();
  });

  it("bounces an unauthenticated visitor to /login", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(null);
    renderPage();
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/login", { replace: true }));
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
