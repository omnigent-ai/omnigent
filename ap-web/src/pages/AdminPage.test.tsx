// Tests for the Admin page (`/admin`) — the OIDC/SSO admin surface that lists
// users and, on selecting one, that user's sessions.
//
// Mocks at the lib seams: `@/lib/identity` (the admin gate) and
// `@/lib/adminApi` (the two discovery endpoints). The page's own assembly /
// selection logic is left real.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminPage } from "./AdminPage";
import * as identity from "@/lib/identity";
import * as adminApi from "@/lib/adminApi";

vi.mock("@/lib/identity", () => ({
  resolveIdentity: vi.fn(),
  getCurrentIsAdmin: vi.fn(),
  getCurrentUserId: vi.fn(),
}));
vi.mock("@/lib/adminApi", () => ({
  listAllUsers: vi.fn(),
  listUserSessions: vi.fn(),
}));
vi.mock("@/lib/routing", () => ({ useNavigate: () => vi.fn() }));

function renderPage() {
  return render(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  );
}

describe("AdminPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(identity.resolveIdentity).mockResolvedValue("boss@example.com");
    vi.mocked(identity.getCurrentUserId).mockReturnValue("boss@example.com");
  });
  afterEach(cleanup);

  it("shows a no-access message for non-admins", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);

    renderPage();

    await waitFor(() => expect(screen.getByText(/don't have admin access/i)).toBeTruthy());
    // The non-admin page never calls the admin API.
    expect(adminApi.listAllUsers).not.toHaveBeenCalled();
  });

  it("lists users for an admin, with role badges and a cost rollup", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
    vi.mocked(adminApi.listAllUsers).mockResolvedValue([
      {
        user_id: "boss@example.com",
        is_admin: true,
        cost_usd: 0,
        total_tokens: 0,
        session_count: 0,
      },
      {
        user_id: "alice@example.com",
        is_admin: false,
        cost_usd: 2,
        total_tokens: 1500,
        session_count: 2,
      },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("alice@example.com")).toBeTruthy());
    expect(screen.getByText("boss@example.com")).toBeTruthy();
    // The current user is annotated.
    expect(screen.getByText("(you)")).toBeTruthy();
    // Role badges + cost rollup, scoped to each row (the page <h1> is "Admin").
    const rows = screen.getAllByTestId("admin-user-row");
    const bossRow = rows.find((r) => within(r).queryByText("boss@example.com"))!;
    const aliceRow = rows.find((r) => within(r).queryByText("alice@example.com"))!;
    expect(within(bossRow).getByText("Admin")).toBeTruthy();
    expect(within(aliceRow).getByText("Member")).toBeTruthy();
    expect(within(aliceRow).getByText("$2.00")).toBeTruthy();
    expect(within(aliceRow).getByText("1.5K")).toBeTruthy();
  });

  it("loads a user's sessions on selection, shows cost, and links into chat", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
    vi.mocked(adminApi.listAllUsers).mockResolvedValue([
      {
        user_id: "alice@example.com",
        is_admin: false,
        cost_usd: 2.5,
        total_tokens: 4200,
        session_count: 1,
      },
    ]);
    vi.mocked(adminApi.listUserSessions).mockResolvedValue({
      sessions: [
        {
          id: "conv_abc",
          title: "Alice's session",
          created_at: 1,
          updated_at: 2,
          cost_usd: 2.5,
          total_tokens: 4200,
        },
      ],
      totals: { cost_usd: 2.5, total_tokens: 4200, session_count: 1 },
    });

    renderPage();

    const row = await screen.findByText("alice@example.com");
    fireEvent.click(row);

    await waitFor(() =>
      expect(adminApi.listUserSessions).toHaveBeenCalledWith("alice@example.com"),
    );
    await waitFor(() => expect(screen.getByText("Alice's session")).toBeTruthy());
    // The sessions panel is headed by the selected user.
    const heading = screen.getByText(/Sessions for/i);
    expect(within(heading).getByText("alice@example.com")).toBeTruthy();
    // Per-session cost is shown in the session row.
    const sessionRow = screen.getByTestId("admin-session-row");
    expect(within(sessionRow).getByText("$2.50")).toBeTruthy();
  });

  it("shows an error state when the user list cannot be loaded", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
    vi.mocked(adminApi.listAllUsers).mockResolvedValue(null);

    renderPage();

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(/Could not load users/i)).toBeTruthy();
  });
});
