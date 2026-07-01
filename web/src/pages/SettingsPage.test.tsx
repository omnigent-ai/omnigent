// Tests for the Settings content panel. The section nav lives in the sidebar
// card (see settingsNav); the page renders only the section named by the URL.
// Covers the Appearance theme picker, the auth-gated Account section, and the
// Archived sessions list (which moved here out of the sidebar).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

const mocks = vi.hoisted(() => ({
  setTheme: vi.fn(),
  theme: "system" as string,
  archiveMutate: vi.fn(),
  deleteMutate: vi.fn(),
  accountsEnabled: true,
  me: { id: "alice", is_admin: false } as { id: string; is_admin: boolean } | null,
  conversations: [] as Conversation[],
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mocks.theme, systemTheme: "light", setTheme: mocks.setTheme }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ accounts_enabled: mocks.accountsEnabled }),
}));
vi.mock("@/lib/accountsApi", () => ({
  getMe: () => Promise.resolve(mocks.me),
  logout: vi.fn(),
  changePassword: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({
    data: { pages: [{ data: mocks.conversations }] },
    isLoading: false,
  }),
  useArchiveConversation: () => ({ mutate: mocks.archiveMutate, isPending: false }),
  useStopAndDeleteConversation: () => ({ mutate: mocks.deleteMutate, isPending: false }),
}));
// The admin management surfaces are lazy-loaded and own heavy data layers of
// their own; stub them so these tests only assert SettingsPage's section
// routing (that /settings/members and /settings/policies render the right one).
vi.mock("@/pages/MembersPage", () => ({
  MembersPage: () => <div>members-page-stub</div>,
}));
vi.mock("@/pages/PoliciesPage", () => ({
  PoliciesPage: () => <div>policies-page-stub</div>,
}));

import { SettingsPage } from "./SettingsPage";

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...partial,
  };
}

function renderPage(path = "/settings") {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mocks.setTheme.mockReset();
  mocks.archiveMutate.mockReset();
  mocks.deleteMutate.mockReset();
  mocks.theme = "system";
  mocks.accountsEnabled = true;
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
});
afterEach(cleanup);

describe("SettingsPage", () => {
  it("renders the Appearance section and applies a theme on card click", () => {
    renderPage("/settings/appearance");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    // System is selected (theme = "system").
    expect(screen.getByTestId("theme-system")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("theme-dark"));
    expect(mocks.setTheme).toHaveBeenCalledWith("dark");
  });

  it("defaults bare /settings to Account when accounts is on, else Appearance", async () => {
    // Accounts on → Account leads, so /settings lands on it.
    renderPage("/settings");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Accounts off → no Account section; default falls back to Appearance.
    cleanup();
    mocks.accountsEnabled = false;
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
  });

  it("renders the Account section at /settings/account when auth is enabled", async () => {
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // With accounts off, the section renders nothing even at its URL.
    cleanup();
    mocks.accountsEnabled = false;
    renderPage("/settings/account");
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("renders the Members section at /settings/members when accounts is on", async () => {
    renderPage("/settings/members");
    expect(await screen.findByText("members-page-stub")).toBeInTheDocument();
    expect(screen.queryByText("policies-page-stub")).toBeNull();
  });

  it("renders the Policies section at /settings/policies when accounts is on", async () => {
    renderPage("/settings/policies");
    expect(await screen.findByText("policies-page-stub")).toBeInTheDocument();
    expect(screen.queryByText("members-page-stub")).toBeNull();
  });

  it("does not render the admin sections when accounts is off", () => {
    mocks.accountsEnabled = false;
    renderPage("/settings/members");
    // Falls through to the section switch, which renders nothing for an
    // unknown-when-accounts-off section.
    expect(screen.queryByText("members-page-stub")).toBeNull();
  });

  it("no longer links to Members / Policies from the Account section", async () => {
    // They moved to the sidebar nav (Admin group); the Account section — even
    // for an admin — must not re-link to them, or we'd be back to navigating
    // away from /settings.
    mocks.me = { id: "alice", is_admin: true };
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: /Members/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Policies/ })).toBeNull();
  });

  it("lists archived sessions and unarchives on click", () => {
    mocks.conversations = [
      conv("conv_active"),
      conv("conv_archived", { archived: true, title: "Old chat" }),
    ];
    renderPage("/settings/archived");

    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Old chat")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("unarchive-conversation"));
    expect(mocks.archiveMutate).toHaveBeenCalledWith({ id: "conv_archived", archived: false });
  });

  it("deletes an archived session after confirming, with no row-click navigation", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    // The row text isn't a link/button target — there's nothing to click into.
    expect(screen.queryByRole("link", { name: /Old chat/ })).toBeNull();

    // Trash → confirm dialog → Delete fires the delete mutation.
    fireEvent.click(screen.getByTestId("delete-archived"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.deleteMutate).toHaveBeenCalledWith({ id: "conv_archived" });
  });
});
