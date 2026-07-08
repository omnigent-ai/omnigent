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
  // login_url: non-null for any sign-in mode (accounts OR OIDC), null in
  // header single-user mode. Gates the Account section.
  loginUrl: "/login" as string | null,
  // Identity from the mode-agnostic `/v1/me` probe (resolveIdentity returns
  // the id, getCurrentIsAdmin the flag). null → unauthenticated.
  me: { id: "alice", is_admin: false } as { id: string; is_admin: boolean } | null,
  conversations: [] as Conversation[],
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mocks.theme, systemTheme: "light", setTheme: mocks.setTheme }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: mocks.accountsEnabled,
    login_url: mocks.loginUrl,
  }),
}));
vi.mock("@/lib/accountsApi", () => ({
  logout: vi.fn(),
  changePassword: vi.fn(),
}));
vi.mock("@/lib/identity", () => ({
  resolveIdentity: () => Promise.resolve(mocks.me?.id ?? null),
  getCurrentIsAdmin: () => mocks.me?.is_admin ?? false,
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
  mocks.loginUrl = "/login";
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
});
afterEach(() => {
  cleanup();
  // Reset the font-size preference + applied scale so the Appearance tests
  // don't leak persisted state or the --ui-font-scale variable into each other.
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
});

describe("SettingsPage", () => {
  it("renders the Appearance section and applies a theme on card click", () => {
    renderPage("/settings/appearance");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    // System is selected (theme = "system").
    expect(screen.getByTestId("theme-system")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("theme-dark"));
    expect(mocks.setTheme).toHaveBeenCalledWith("dark");
  });

  it("renders the Terminal theme radiogroup with auto selected by default", () => {
    renderPage("/settings/appearance");
    expect(screen.getByRole("radiogroup", { name: "Terminal theme" })).toBeInTheDocument();
    expect(screen.getByTestId("terminal-theme-auto")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("terminal-theme-light")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("terminal-theme-dark")).toHaveAttribute("aria-checked", "false");
    expect(localStorage.getItem("omnigent:terminal-theme")).toBeNull();
  });

  it("persists dark and light terminal theme choices on card click", () => {
    renderPage("/settings/appearance");

    fireEvent.click(screen.getByTestId("terminal-theme-dark"));
    expect(localStorage.getItem("omnigent:terminal-theme")).toBe("dark");
    expect(screen.getByTestId("terminal-theme-dark")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("terminal-theme-auto")).toHaveAttribute("aria-checked", "false");

    fireEvent.click(screen.getByTestId("terminal-theme-light"));
    expect(localStorage.getItem("omnigent:terminal-theme")).toBe("light");
    expect(screen.getByTestId("terminal-theme-light")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("terminal-theme-dark")).toHaveAttribute("aria-checked", "false");
  });

  it("reflects a stored light terminal theme on mount", () => {
    localStorage.setItem("omnigent:terminal-theme", "light");
    renderPage("/settings/appearance");
    expect(screen.getByTestId("terminal-theme-light")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("terminal-theme-auto")).toHaveAttribute("aria-checked", "false");
  });

  it("shows the default UI font size and steps it up, persisting the choice", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    // No stored preference → 16px default.
    expect(input.value).toBe("16");

    fireEvent.click(screen.getByTestId("ui-font-size-inc"));
    expect(input.value).toBe("17");
    // The choice is persisted so it survives a refresh.
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("17");
    // The scale is applied live to the document root (17 / 16).
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.0625");
  });

  it("disables the steppers at the min and max bounds", () => {
    localStorage.setItem("omnigent:ui-font-size", "20");
    renderPage("/settings/appearance");
    // At the 20px max, only the increase button is disabled.
    expect(screen.getByTestId("ui-font-size-inc")).toBeDisabled();
    expect(screen.getByTestId("ui-font-size-dec")).not.toBeDisabled();

    cleanup();
    localStorage.setItem("omnigent:ui-font-size", "12");
    renderPage("/settings/appearance");
    // At the 12px min, only the decrease button is disabled.
    expect(screen.getByTestId("ui-font-size-dec")).toBeDisabled();
    expect(screen.getByTestId("ui-font-size-inc")).not.toBeDisabled();
  });

  it("shows the empty font family default and applies + persists a typed name", () => {
    localStorage.clear();
    document.documentElement.style.removeProperty("--ui-font-family");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-family-input") as HTMLInputElement;
    // No stored preference → empty input, System-default placeholder, no override.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("System default");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    // Reset has nothing to do at the default.
    expect(screen.getByTestId("ui-font-family-reset")).toBeDisabled();

    fireEvent.change(input, { target: { value: "Inter" } });
    expect(input.value).toBe("Inter");
    // The choice is persisted so it survives a refresh...
    expect(localStorage.getItem("omnigent:ui-font-family")).toBe(JSON.stringify("Inter"));
    // ...and applied live to the document root, with the system stack appended
    // so an uninstalled/partial name degrades to the default sans, not serif.
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
    expect(screen.getByTestId("ui-font-family-reset")).not.toBeDisabled();
  });

  it("reset restores the system default font family", () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Georgia"));
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-family-input") as HTMLInputElement;
    // The control reflects the stored preference on mount.
    expect(input.value).toBe("Georgia");

    fireEvent.click(screen.getByTestId("ui-font-family-reset"));
    // Reset clears the field, the applied property, and the stored key.
    expect(input.value).toBe("");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    expect(localStorage.getItem("omnigent:ui-font-family")).toBeNull();
  });

  it("lets you clear and retype the font size without clamping mid-edit", () => {
    localStorage.setItem("omnigent:ui-font-size", "13");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    expect(input.value).toBe("13");

    // Deleting a digit leaves "1" — below the 12px min. The box must SHOW "1"
    // (free editing) without snapping to 12 or persisting the transient value.
    fireEvent.change(input, { target: { value: "1" } });
    expect(input.value).toBe("1");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("13");
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("");

    // Finishing the number to a valid size applies it live and persists it.
    fireEvent.change(input, { target: { value: "18" } });
    expect(input.value).toBe("18");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("18");
    // 18 / 16 base = 1.125.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.125");
  });

  it("clamps a below-min entry to the minimum on blur", () => {
    localStorage.setItem("omnigent:ui-font-size", "16");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.blur(input);
    // On blur the draft settles to the clamped minimum.
    expect(input.value).toBe("12");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("12");
  });

  it("reverts an empty entry to the committed size on blur", () => {
    localStorage.setItem("omnigent:ui-font-size", "15");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "" } });
    expect(input.value).toBe("");
    fireEvent.blur(input);
    // An empty field restores the last committed value rather than a bogus one.
    expect(input.value).toBe("15");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("15");
  });

  it("shows the default code font size and steps it up, persisting the choice", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const input = screen.getByTestId("code-font-size-input") as HTMLInputElement;
    // No stored preference → 13px default (code widgets read a touch smaller
    // than the 16px chrome default).
    expect(input.value).toBe("13");

    fireEvent.click(screen.getByTestId("code-font-size-inc"));
    expect(input.value).toBe("14");
    // Persisted under the code-font key (distinct from the chrome font's) so it
    // survives a refresh. There's no --ui-font-scale here — the pref reaches the
    // editor/terminal imperatively, not via a CSS variable.
    expect(localStorage.getItem("omnigent:code-font-size")).toBe("14");
  });

  it("disables the code font steppers at the min and max bounds", () => {
    localStorage.setItem("omnigent:code-font-size", "24");
    renderPage("/settings/appearance");
    // At the 24px max, only the increase button is disabled.
    expect(screen.getByTestId("code-font-size-inc")).toBeDisabled();
    expect(screen.getByTestId("code-font-size-dec")).not.toBeDisabled();

    cleanup();
    localStorage.setItem("omnigent:code-font-size", "10");
    renderPage("/settings/appearance");
    // At the 10px min, only the decrease button is disabled.
    expect(screen.getByTestId("code-font-size-dec")).toBeDisabled();
    expect(screen.getByTestId("code-font-size-inc")).not.toBeDisabled();
  });

  it("lets you clear and retype the code font size, clamping below-min on blur", () => {
    localStorage.setItem("omnigent:code-font-size", "13");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("code-font-size-input") as HTMLInputElement;
    expect(input.value).toBe("13");

    // Backspacing to "1" is below the 10px min: the box SHOWS "1" (free editing)
    // without snapping or persisting the transient value.
    fireEvent.change(input, { target: { value: "1" } });
    expect(input.value).toBe("1");
    expect(localStorage.getItem("omnigent:code-font-size")).toBe("13");

    // Finishing to a valid size applies + persists it.
    fireEvent.change(input, { target: { value: "20" } });
    expect(input.value).toBe("20");
    expect(localStorage.getItem("omnigent:code-font-size")).toBe("20");

    // A still-out-of-range draft clamps to the minimum on blur.
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.blur(input);
    expect(input.value).toBe("10");
    expect(localStorage.getItem("omnigent:code-font-size")).toBe("10");
  });

  it("shows the empty code font family default and applies + persists a typed name", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const input = screen.getByTestId("code-font-family-input") as HTMLInputElement;
    // No stored preference → empty input, editor-default placeholder.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("Editor default");
    // Reset has nothing to do at the default.
    expect(screen.getByTestId("code-font-family-reset")).toBeDisabled();

    fireEvent.change(input, { target: { value: "Fira Code" } });
    expect(input.value).toBe("Fira Code");
    // The choice is persisted under the code-font family key so it survives a refresh.
    expect(localStorage.getItem("omnigent:code-font-family")).toBe(JSON.stringify("Fira Code"));
    expect(screen.getByTestId("code-font-family-reset")).not.toBeDisabled();
  });

  it("reset restores the default code font family", () => {
    localStorage.setItem("omnigent:code-font-family", JSON.stringify("JetBrains Mono"));
    renderPage("/settings/appearance");
    const input = screen.getByTestId("code-font-family-input") as HTMLInputElement;
    // The control reflects the stored preference on mount.
    expect(input.value).toBe("JetBrains Mono");

    fireEvent.click(screen.getByTestId("code-font-family-reset"));
    // Reset clears the field and the stored key.
    expect(input.value).toBe("");
    expect(localStorage.getItem("omnigent:code-font-family")).toBeNull();
  });

  it("defaults bare /settings to Account when a login session exists, else Appearance", async () => {
    // Login session (accounts OR OIDC) → Account leads, so /settings lands on it.
    renderPage("/settings");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Header single-user (no login_url) → no Account section; falls back to
    // Appearance.
    cleanup();
    mocks.accountsEnabled = false;
    mocks.loginUrl = null;
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
  });

  it("renders the Account section at /settings/account for any login session", async () => {
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Header single-user (no login_url) → the section renders nothing even at
    // its URL.
    cleanup();
    mocks.accountsEnabled = false;
    mocks.loginUrl = null;
    renderPage("/settings/account");
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("renders the Account section under OIDC (accounts off, login_url set)", async () => {
    // #1489: an SSO user must be able to see their identity and sign out.
    mocks.accountsEnabled = false;
    mocks.loginUrl = "/auth/login";
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    // Change password is accounts-only — hidden under OIDC.
    expect(screen.queryByRole("button", { name: /Change password/ })).toBeNull();
    // Sign out is still available.
    expect(screen.getByRole("button", { name: /Sign out/ })).toBeInTheDocument();
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

  it("still renders the admin sections when accounts is off (OIDC)", async () => {
    // #1489: Members / Policies are admin surfaces valid under OIDC too. The
    // page itself self-gates to admins (and runs read-only under OIDC); the
    // SettingsPage no longer withholds the section based on accounts_enabled.
    mocks.accountsEnabled = false;
    renderPage("/settings/members");
    expect(await screen.findByText("members-page-stub")).toBeInTheDocument();
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
