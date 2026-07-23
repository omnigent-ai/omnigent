// Tests for the Settings content panel. The section nav lives in the sidebar
// card (see settingsNav); the page renders only the section named by the URL.
// Covers the Appearance theme picker, the auth-gated Account section, and the
// Archived sessions list (which moved here out of the sidebar).

import { type ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { ElectronUpdateBridge, UpdateConfig, UpdateStatus } from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  setTheme: vi.fn(),
  theme: "system" as string,
  archiveMutate: vi.fn(),
  deleteMutate: vi.fn(),
  accountsEnabled: true,
  // login_url: non-null for any sign-in mode (accounts OR OIDC), null in
  // header mode. Gates the Account section.
  loginUrl: "/login" as string | null,
  // single_user: explicit single-user marker; false for accounts/OIDC/
  // multi-user-header. Gates the settings-route single-user redirect.
  singleUser: false,
  // Identity from the mode-agnostic `/v1/me` probe (resolveIdentity returns
  // the id, getCurrentIsAdmin the flag). null → unauthenticated.
  me: { id: "alice", is_admin: false } as { id: string; is_admin: boolean } | null,
  conversations: [] as Conversation[],
  // Optional multi-page dataset (array of per-page row arrays) for pagination
  // tests. When unset the mock serves a single page of `conversations`.
  pages: undefined as Conversation[][] | undefined,
  // Picker options come from useArchivedProjectNames (a dedicated scan), not
  // from the loaded rows — so tests set them independently of `conversations`.
  projectNames: [] as string[],
  hasNextPage: false,
  fetchNextPage: vi.fn(),
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mocks.theme, systemTheme: "light", setTheme: mocks.setTheme }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: mocks.accountsEnabled,
    login_url: mocks.loginUrl,
    single_user: mocks.singleUser,
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
vi.mock("@/hooks/useConversations", async () => {
  // A stateful mock that emulates useInfiniteQuery pagination: it tracks how
  // many pages are "loaded" and reveals the next on fetchNextPage, so a click
  // on "Load more" re-renders with more rows (as the real hook would).
  const { useState } = await import("react");
  return {
    PROJECT_LABEL_KEY: "omni_project",
    // The Archived view drives the visible list from this hook; filter on the
    // fourth (`project`) arg so the mock mirrors the server-side ?project=
    // scoping.
    useConversations: (
      _searchQuery?: string,
      _includeArchived?: boolean,
      _options?: unknown,
      project?: string,
    ) => {
      // `mocks.pages` (array of per-page row arrays) drives multi-page tests;
      // otherwise serve a single page of `mocks.conversations`.
      const source = mocks.pages ?? [mocks.conversations];
      const [shown, setShown] = useState(1);
      const pages = source.slice(0, shown).map((rows) => ({
        data: project ? rows.filter((c) => c.labels?.["omni_project"] === project) : rows,
      }));
      return {
        data: { pages },
        isLoading: false,
        hasNextPage: shown < source.length || mocks.hasNextPage,
        isFetchingNextPage: false,
        fetchNextPage: () => {
          mocks.fetchNextPage();
          setShown((n) => Math.min(n + 1, source.length));
        },
      };
    },
    // Picker options are sourced from this dedicated scan, decoupled from the
    // loaded rows so archived-only projects on later pages still appear.
    useArchivedProjectNames: () => ({ data: mocks.projectNames }),
    useArchiveConversation: () => ({ mutate: mocks.archiveMutate, isPending: false }),
    useStopAndDeleteConversation: () => ({ mutate: mocks.deleteMutate, isPending: false }),
  };
});
// Radix Select uses a portal + pointer events jsdom can't drive; stub it to a
// native <select> so tests can drive both the color-theme dropdown and the
// archived project filter. The real page puts data-testid on SelectTrigger,
// so the stub lifts it from the trigger child onto the native <select>.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((c) => isValidElement(c) && c.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && trigger.props && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
      >
        {kids.filter((c) => !(isValidElement(c) && c.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => <>{children}</>,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});
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
  mocks.fetchNextPage.mockReset();
  mocks.theme = "system";
  mocks.accountsEnabled = true;
  mocks.loginUrl = "/login";
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
  mocks.pages = undefined;
  mocks.projectNames = [];
  mocks.hasNextPage = false;
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});
afterEach(() => {
  cleanup();
  // Reset the font-size preference + applied scale so the Appearance tests
  // don't leak persisted state or the --ui-font-scale variable into each other.
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
  document.documentElement.style.removeProperty("--sidebar-font-size");
  // The palette picker sets data-theme on <html>; clear it so a palette
  // selected in one test doesn't leak into the next.
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-custom-translucent-sidebar");
  for (const property of Array.from(document.documentElement.style)) {
    if (property.startsWith("--custom-")) document.documentElement.style.removeProperty(property);
  }
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

const DEFAULT_UPDATE_CONFIG: UpdateConfig = {
  mode: "default",
  autoInstall: true,
  skippedVersion: null,
};

function installUpdateBridge(config: UpdateConfig = DEFAULT_UPDATE_CONFIG) {
  let onStatus: Parameters<ElectronUpdateBridge["onStatus"]>[0] | null = null;
  const unsubscribe = vi.fn();
  const bridge: ElectronUpdateBridge = {
    getConfig: vi.fn().mockResolvedValue(config),
    getStatus: vi.fn().mockResolvedValue({ state: "idle" }),
    check: vi.fn().mockResolvedValue(undefined),
    download: vi.fn().mockResolvedValue(undefined),
    installNow: vi.fn().mockResolvedValue(undefined),
    setConfig: vi.fn().mockImplementation((patch: Partial<UpdateConfig>) =>
      Promise.resolve({
        ...config,
        ...patch,
      }),
    ),
    onStatus: vi.fn((cb) => {
      onStatus = cb;
      return unsubscribe;
    }),
  };
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: vi.fn(),
    notify: vi.fn(),
    updates: bridge,
  };
  return {
    bridge,
    emitStatus: (status: UpdateStatus) => onStatus?.(status),
    unsubscribe,
  };
}

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

  it("renders Terminal theme before Color theme", () => {
    renderPage("/settings/appearance");
    const terminal = screen.getByText("Terminal theme");
    const color = screen.getByText("Color theme");
    expect(terminal.compareDocumentPosition(color) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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

  it("renders the color theme dropdown, defaults to Omnigent, and applies a palette on change", () => {
    localStorage.clear();
    renderPage("/settings/appearance");

    const select = screen.getByTestId("color-theme-select") as HTMLSelectElement;
    // Nothing stored → the default (Omnigent) palette is selected and no
    // data-theme override is applied to the document.
    expect(select.value).toBe("omni");
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();

    // Choosing a palette applies it live to <html> and persists it.
    fireEvent.change(select, { target: { value: "github" } });
    expect(select.value).toBe("github");
    expect(document.documentElement.getAttribute("data-theme")).toBe("github");
    expect(localStorage.getItem("omnigent:ui-theme-palette")).toBe(JSON.stringify("github"));
  });

  it("creates and applies a custom theme when a guided color control changes", () => {
    renderPage("/settings/appearance");
    const select = screen.getByTestId("color-theme-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "github" } });

    fireEvent.click(screen.getByTestId("custom-theme-accent-trigger"));
    const accent = screen.getByTestId("custom-theme-accent-input") as HTMLInputElement;
    expect(accent.value).toBe("#0969DA");
    fireEvent.change(accent, { target: { value: "#2563eb" } });

    expect(select.value).toBe("custom");
    expect(document.documentElement.getAttribute("data-theme")).toBe("custom");
    expect(localStorage.getItem("omnigent:ui-theme-palette")).toBe(JSON.stringify("custom"));
    expect(JSON.parse(localStorage.getItem("omnigent:custom-theme") ?? "null")).toMatchObject({
      basePalette: "github",
      accent: "#2563eb",
    });
    expect(document.documentElement.style.getPropertyValue("--custom-light-primary")).toBe(
      "#2563eb",
    );
  });

  it("persists the shared contrast and translucent-sidebar controls", () => {
    renderPage("/settings/appearance");

    fireEvent.change(screen.getByTestId("custom-theme-contrast"), {
      target: { value: "68" },
    });
    fireEvent.click(screen.getByTestId("custom-theme-translucent-sidebar"));

    expect(screen.getByTestId("color-theme-select")).toHaveValue("custom");
    expect(screen.getByTestId("custom-theme-contrast-value")).toHaveTextContent("68");
    expect(JSON.parse(localStorage.getItem("omnigent:custom-theme") ?? "null")).toMatchObject({
      contrast: 68,
      translucentSidebar: true,
    });
    expect(document.documentElement.style.getPropertyValue("--custom-light-sidebar")).toMatch(
      /^rgba\(/,
    );
    expect(document.documentElement).toHaveAttribute("data-custom-translucent-sidebar");
  });

  it("moves the mode selection with arrow keys (radiogroup keyboard nav)", () => {
    renderPage("/settings/appearance");

    // Arrow keys move within the mode radiogroup and select as focus moves (the
    // WAI-ARIA radiogroup pattern). themeCards order is System / Light / Dark,
    // so ArrowRight from System selects Light.
    const system = screen.getByTestId("theme-system");
    system.focus();
    fireEvent.keyDown(system, { key: "ArrowRight" });

    expect(mocks.setTheme).toHaveBeenCalledWith("light");
  });

  it("groups a Sidebar-only font size control under Appearance", () => {
    renderPage("/settings/appearance");

    const sidebarGroup = screen.getByRole("group", { name: "Sidebar settings" });
    expect(within(sidebarGroup).getByText("Sidebar")).toBeInTheDocument();
    const input = within(sidebarGroup).getByLabelText(
      "Sidebar font size in pixels",
    ) as HTMLInputElement;
    expect(input.value).toBe("13");

    fireEvent.click(within(sidebarGroup).getByLabelText("Increase sidebar font size"));

    expect(input.value).toBe("14");
    expect(localStorage.getItem("omnigent:sidebar-font-size")).toBe("14");
    expect(document.documentElement.style.getPropertyValue("--sidebar-font-size")).toBe("14px");
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("");
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

  it("persists an Updates mode change through the desktop bridge", async () => {
    const { bridge } = installUpdateBridge();

    renderPage("/settings/updates");
    expect(await screen.findByRole("heading", { name: "Updates" })).toBeInTheDocument();

    const select = screen.getByRole("combobox", { name: "Update mode" }) as HTMLSelectElement;
    expect(select.value).toBe("default");
    fireEvent.change(select, { target: { value: "manual" } });

    await waitFor(() => {
      expect(bridge.setConfig).toHaveBeenCalledWith({ mode: "manual" });
    });
  });

  it("surfaces manual update-check failures in Settings", async () => {
    const { bridge, emitStatus } = installUpdateBridge();
    vi.mocked(bridge.check).mockRejectedValueOnce(new Error("Cannot find latest.yml: 404"));

    renderPage("/settings/updates");
    expect(await screen.findByRole("heading", { name: "Updates" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Check for updates now" }));

    expect(await screen.findByText("Last check failed")).toBeInTheDocument();
    expect(screen.getByText("Cannot find latest.yml: 404")).toBeInTheDocument();

    emitStatus({ state: "checking" });
    await waitFor(() => {
      expect(screen.queryByText("Cannot find latest.yml: 404")).toBeNull();
    });

    emitStatus({ state: "idle", lastError: "Feed provider failed" });
    expect(await screen.findByText("Feed provider failed")).toBeInTheDocument();
  });

  it("unsubscribes from update status events when Settings unmounts", async () => {
    const { unsubscribe } = installUpdateBridge();

    const { unmount } = renderPage("/settings/updates");
    expect(await screen.findByRole("heading", { name: "Updates" })).toBeInTheDocument();

    unmount();

    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("hides the Updates section outside the Electron shell", () => {
    renderPage("/settings/updates");
    expect(screen.queryByRole("heading", { name: "Updates" })).toBeNull();
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

  it("shows an empty default base branch by default and persists a typed value", () => {
    localStorage.clear();
    renderPage("/settings/git");
    expect(screen.getByRole("heading", { name: "Git" })).toBeInTheDocument();
    const input = screen.getByTestId("settings-default-base-branch-input") as HTMLInputElement;
    // Nothing stored → blank field, so the composer won't auto-fill.
    expect(input.value).toBe("");

    fireEvent.change(input, { target: { value: "main" } });
    expect(input.value).toBe("main");
    // The choice persists so the composer can read it on the next new branch.
    expect(localStorage.getItem("omnigent:default-base-branch")).toBe("main");
  });

  it("reflects a stored default base branch on mount", () => {
    localStorage.setItem("omnigent:default-base-branch", "develop");
    renderPage("/settings/git");
    const input = screen.getByTestId("settings-default-base-branch-input") as HTMLInputElement;
    expect(input.value).toBe("develop");
  });

  it("clears the default base branch preference when emptied", () => {
    localStorage.setItem("omnigent:default-base-branch", "main");
    renderPage("/settings/git");
    const input = screen.getByTestId("settings-default-base-branch-input") as HTMLInputElement;
    expect(input.value).toBe("main");

    // Emptying the field turns auto-fill off — the key is removed, not stored blank.
    fireEvent.change(input, { target: { value: "" } });
    expect(input.value).toBe("");
    expect(localStorage.getItem("omnigent:default-base-branch")).toBeNull();
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

  it("scopes the archived list to the project picked in the filter", () => {
    mocks.projectNames = ["Alpha", "Beta"];
    mocks.conversations = [
      conv("conv_a", { archived: true, title: "Alpha chat", labels: { omni_project: "Alpha" } }),
      conv("conv_b", { archived: true, title: "Beta chat", labels: { omni_project: "Beta" } }),
      conv("conv_active"),
    ];
    renderPage("/settings/archived");

    // "All projects" (default) lists every archived session.
    expect(screen.getAllByTestId("archived-row")).toHaveLength(2);
    const select = screen.getByTestId("archived-project-filter");
    expect(within(select).getByRole("option", { name: "All projects" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Alpha" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Beta" })).toBeInTheDocument();

    // Picking a project narrows the list to that project's archived sessions.
    // Select values are discriminated (`project:<name>`), never the raw name.
    fireEvent.change(select, { target: { value: "project:Alpha" } });
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Alpha chat")).toBeInTheDocument();

    // Back to "All projects" restores the full list.
    fireEvent.change(select, { target: { value: "all" } });
    expect(screen.getAllByTestId("archived-row")).toHaveLength(2);
  });

  it("hides the project filter when no archived session belongs to a project", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    expect(screen.queryByTestId("archived-project-filter")).toBeNull();
    expect(screen.getByTestId("archived-row")).toBeInTheDocument();
  });

  it("shows the empty state (and no filter) when there are no archived sessions", () => {
    mocks.conversations = [conv("conv_active")];
    renderPage("/settings/archived");

    expect(screen.getByText("No archived sessions.")).toBeInTheDocument();
    expect(screen.queryByTestId("archived-project-filter")).toBeNull();
  });

  it("shows a project-scoped empty state when the picked project has no rows", () => {
    mocks.projectNames = ["Alpha"];
    mocks.conversations = [
      conv("conv_a", { archived: true, title: "Alpha chat", labels: { omni_project: "Alpha" } }),
    ];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Drop Alpha's only session so the filtered fetch returns nothing, then
    // pick Alpha (still an option because it's in the scanned name set).
    mocks.conversations = [];
    fireEvent.change(select, { target: { value: "project:Alpha" } });
    expect(screen.getByText("No archived sessions in this project.")).toBeInTheDocument();
  });

  it("offers archived-only projects whose sessions are beyond the first loaded page", () => {
    // The visible list's first page has no Gamma row, but the option scan
    // (useArchivedProjectNames, which pages through everything) found Gamma —
    // this is the gotcha the feature exists for.
    mocks.projectNames = ["Gamma"];
    mocks.conversations = [conv("p1", { archived: true, title: "Page-one chat" })];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Gamma is offered even though no Gamma row is in the loaded page.
    expect(within(select).getByRole("option", { name: "Gamma" })).toBeInTheDocument();
  });

  it("treats a project literally named __all__ as a real project, not the clear-filter sentinel", () => {
    mocks.projectNames = ["Other", "__all__"];
    mocks.conversations = [
      conv("x1", { archived: true, title: "Edge chat", labels: { omni_project: "__all__" } }),
      conv("o1", { archived: true, title: "Other chat", labels: { omni_project: "Other" } }),
    ];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Picking the "__all__" project must FILTER to it (discriminated value
    // `project:__all__`), not clear the filter.
    fireEvent.change(select, { target: { value: "project:__all__" } });
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Edge chat")).toBeInTheDocument();
  });

  it("loads the next page of archived sessions on demand", () => {
    mocks.conversations = [conv("a1", { archived: true, title: "Old chat" })];
    mocks.hasNextPage = true;
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-load-more"));
    expect(mocks.fetchNextPage).toHaveBeenCalled();
  });

  it("keeps Load more available when page 1 has only active rows, then pages to archived", () => {
    // The first page holds only active sessions (archived ones sort onto a
    // later page). This must NOT dead-end on the definitive empty state.
    mocks.pages = [
      [conv("act1", { title: "Active chat" })],
      [conv("arch2", { archived: true, title: "Deep archive" })],
    ];
    renderPage("/settings/archived");

    // No archived rows on page 1, but more pages exist → not the definitive
    // empty state; a pager is offered instead.
    expect(screen.queryByText("No archived sessions.")).toBeNull();
    expect(screen.getByText("No archived sessions on this page.")).toBeInTheDocument();
    expect(screen.getByTestId("archived-load-more")).toBeInTheDocument();

    // Paging forward surfaces the archived row that lived on page 2.
    fireEvent.click(screen.getByTestId("archived-load-more"));
    expect(mocks.fetchNextPage).toHaveBeenCalled();
    expect(screen.getByTestId("archived-row")).toBeInTheDocument();
    expect(screen.getByText("Deep archive")).toBeInTheDocument();
  });
});
