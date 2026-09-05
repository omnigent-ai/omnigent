// Tests for the Settings content panel. The section nav lives in the sidebar
// card (see settingsNav); the page renders only the section named by the URL.
// Covers the Appearance theme picker, the auth-gated Account section, and the
// Archived sessions list (which moved here out of the sidebar).

import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { ElectronUpdateBridge, UpdateConfig, UpdateStatus } from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  setTheme: vi.fn(),
  theme: "system" as string,
  archiveMutate: vi.fn(),
  deleteMutate: vi.fn(),
  bulkArchiveMutate: vi.fn(),
  bulkDeleteMutate: vi.fn(),
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
  // Optional page dataset for pagination tests. Only the page selected by the
  // hook cursor is returned, matching the production bounded-page contract.
  pages: undefined as Conversation[][] | undefined,
  // Picker options come from the Server aggregate endpoint, independently of
  // the currently visible page.
  projectNames: [] as string[],
  hostIds: [] as string[],
  agentNames: [] as string[],
  hosts: [] as { host_id: string; name: string }[],
  archivedFilters: undefined as Record<string, unknown> | undefined,
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
  getCurrentUserId: () => mocks.me?.id ?? null,
}));
vi.mock("@/hooks/useConversations", async () => {
  return {
    PROJECT_LABEL_KEY: "omni_project",
    // The Archived view drives the visible list from this hook; filter on the
    // fourth (`project`) arg so the mock mirrors the server-side ?project=
    // scoping.
    useArchivedConversations: (
      filters: {
        project?: string;
        hostId?: string;
        agentName?: string;
        searchQuery?: string;
      },
      after?: string,
    ) => {
      mocks.archivedFilters = filters;
      const source = mocks.pages ?? [mocks.conversations];
      const previousIndex = after ? source.findIndex((rows) => rows.at(-1)?.id === after) : -1;
      const pageIndex = previousIndex + 1;
      const rows = source[pageIndex] ?? [];
      const data = rows.filter((conversation) => {
        if (conversation.archived !== true) return false;
        if (filters.project && conversation.labels?.["omni_project"] !== filters.project) {
          return false;
        }
        if (filters.hostId && conversation.host_id !== filters.hostId) return false;
        if (filters.agentName && conversation.agent_name !== filters.agentName) return false;
        const query = filters.searchQuery?.toLowerCase();
        return (
          !query ||
          `${conversation.title ?? ""} ${conversation.workspace ?? ""}`
            .toLowerCase()
            .includes(query)
        );
      });
      return {
        data: {
          data,
          first_id: data.at(0)?.id ?? null,
          last_id: data.at(-1)?.id ?? null,
          has_more: pageIndex < source.length - 1,
        },
        isLoading: false,
        isFetching: false,
      };
    },
    // Picker options are sourced from this dedicated scan, decoupled from the
    // loaded rows so archived-only projects on later pages still appear.
    useArchivedSessionFacets: () => ({
      data: { projects: mocks.projectNames, hostIds: mocks.hostIds, agentNames: mocks.agentNames },
    }),
    useProjects: () => ({ data: [] }),
    useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
    // Mirrors react-query's mutate: per-call `onSuccess` runs once the
    // mutation settles, which is what drives the post-unarchive navigation.
    useArchiveConversation: () => ({
      mutate: (vars: { id: string; archived: boolean }, opts?: { onSuccess?: () => void }) => {
        mocks.archiveMutate(vars, opts);
        opts?.onSuccess?.();
      },
      isPending: false,
    }),
    useStopAndDeleteConversation: () => ({ mutate: mocks.deleteMutate, isPending: false }),
    useBulkArchiveConversations: () => ({
      mutate: mocks.bulkArchiveMutate,
      isPending: false,
      isError: false,
    }),
    useBulkDeleteConversations: () => ({
      mutate: mocks.bulkDeleteMutate,
      isPending: false,
      isError: false,
    }),
  };
});
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: mocks.hosts }),
}));
vi.mock("@/components/archive/ArchiveTranscriptViewer", () => ({
  ArchiveTranscriptViewer: ({ conversation }: { conversation: Conversation | null }) => (
    <div data-testid="archive-transcript" tabIndex={0}>
      {conversation ? `Transcript: ${conversation.title ?? conversation.id}` : "Select a session"}
    </div>
  ),
}));
// Radix Select uses a portal + pointer events jsdom can't drive; stub it to a
// native <select> so tests can drive both the color-theme dropdown and the
// archived project filter. The real page puts data-testid on SelectTrigger,
// so the stub lifts it from the trigger child onto the native <select>.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => children;
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
    SelectContent: ({ children }: { children: ReactNode }) => children,
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

/** Exposes the router location so navigation assertions read the real URL. */
function LocationProbe() {
  return <span data-testid="location">{useLocation().pathname}</span>;
}

function renderPage(path = "/settings") {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
        <LocationProbe />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

function chooseArchiveFilter(label: string, option: string) {
  fireEvent.click(screen.getByRole("combobox", { name: label }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}

function useMobileViewport(): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query === "(max-width: 767.98px)",
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

beforeEach(() => {
  mocks.setTheme.mockReset();
  mocks.archiveMutate.mockReset();
  mocks.deleteMutate.mockReset();
  mocks.bulkArchiveMutate.mockReset();
  mocks.bulkDeleteMutate.mockReset();
  mocks.theme = "system";
  mocks.accountsEnabled = true;
  mocks.loginUrl = "/login";
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
  mocks.pages = undefined;
  mocks.projectNames = [];
  mocks.hostIds = [];
  mocks.agentNames = [];
  mocks.hosts = [];
  mocks.archivedFilters = undefined;
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});
afterEach(() => {
  cleanup();
  // Reset the font-size preference + applied desktop size so the Appearance
  // tests don't leak state into each other.
  localStorage.clear();
  document.documentElement.style.removeProperty("--desktop-ui-font-size");
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
  it("renders composer shortcut guidance as two accessible lines", () => {
    renderPage("/settings/general");
    const toggle = screen.getByTestId("composer-submit-with-mod-enter-toggle");
    const descriptionId = toggle.getAttribute("aria-describedby");
    const description = descriptionId ? document.getElementById(descriptionId) : null;

    expect(description).toBeTruthy();
    if (description === null) throw new Error("Missing composer shortcut description");
    expect(Array.from(description.children).map((line) => line.tagName)).toEqual(["P", "P"]);
    expect(
      within(description).getByText("Off: Enter submits and Shift+Enter inserts a newline."),
    ).toBeInTheDocument();
    expect(
      within(description).getByText(/On: Enter inserts a newline and (?:⌘|Ctrl)\+Enter submits\./),
    ).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-labelledby");
    expect(toggle).toHaveAccessibleName(/Submit with (?:⌘|Ctrl) \+ Enter on desktop/);
  });

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

  it("defaults transcripts to Chat and persists a Terminal default", () => {
    renderPage("/settings/appearance");

    expect(screen.getByRole("radiogroup", { name: "Default transcript view" })).toBeInTheDocument();
    expect(screen.getByTestId("transcript-view-default-chat")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(localStorage.getItem("omnigent:default-transcript-view")).toBeNull();

    fireEvent.click(screen.getByTestId("transcript-view-default-terminal"));
    expect(screen.getByTestId("transcript-view-default-terminal")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(localStorage.getItem("omnigent:default-transcript-view")).toBe("terminal");
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
    expect(accent.value).toBe("#1F883D");
    fireEvent.change(accent, { target: { value: "#2563eb" } });

    expect(select.value).toBe("custom");
    expect(document.documentElement.getAttribute("data-theme")).toBe("custom");
    expect(localStorage.getItem("omnigent:ui-theme-palette")).toBe(JSON.stringify("custom"));
    expect(JSON.parse(localStorage.getItem("omnigent:custom-theme") ?? "null")).toMatchObject({
      basePalette: "github",
      accent: "#2563eb",
      darkAccent: "#2563eb",
    });
    expect(document.documentElement.style.getPropertyValue("--custom-light-primary")).toBe(
      "#2563eb",
    );
  });

  it("keeps the preset primary color when contrast creates a custom theme", () => {
    renderPage("/settings/appearance");
    fireEvent.change(screen.getByTestId("color-theme-select"), {
      target: { value: "github" },
    });

    fireEvent.change(screen.getByTestId("custom-theme-contrast"), {
      target: { value: "68" },
    });

    expect(document.documentElement.getAttribute("data-theme")).toBe("custom");
    expect(document.documentElement.style.getPropertyValue("--custom-light-primary")).toBe(
      "#1f883d",
    );
    expect(document.documentElement.style.getPropertyValue("--custom-dark-primary")).toBe(
      "#238636",
    );
    expect(document.documentElement.style.getPropertyValue("--custom-dark-background")).toBe(
      "#0d1117",
    );
  });

  it("restores Dracula surfaces when contrast returns to 50", () => {
    renderPage("/settings/appearance");
    fireEvent.change(screen.getByTestId("color-theme-select"), {
      target: { value: "dracula" },
    });

    const contrast = screen.getByTestId("custom-theme-contrast");
    fireEvent.change(contrast, { target: { value: "53" } });
    fireEvent.change(contrast, { target: { value: "50" } });

    const style = document.documentElement.style;
    expect(style.getPropertyValue("--custom-light-background")).toBe("#f7f5fd");
    expect(style.getPropertyValue("--custom-light-card")).toBe("#ffffff");
    expect(style.getPropertyValue("--custom-light-sidebar")).toBe("#f3f0fa");
    expect(style.getPropertyValue("--custom-light-border")).toBe("#e6e0f2");
    expect(style.getPropertyValue("--custom-light-brand-accent")).toBe("#d6409f");
  });

  it("persists the shared contrast and translucent-sidebar controls", () => {
    renderPage("/settings/appearance");

    fireEvent.change(screen.getByTestId("custom-theme-contrast"), {
      target: { value: "68" },
    });
    fireEvent.click(screen.getByTestId("custom-theme-translucent-sidebar"));

    expect(screen.getByTestId("custom-theme-contrast")).toHaveStyle({
      "--range-progress": "68%",
    });
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

  it("shows the default UI font size and steps it up, persisting the choice", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    // No stored preference → 13px default.
    expect(input.value).toBe("13");
    expect(screen.getByTestId("ui-font-size-inc").querySelector("svg")).toHaveClass("ui-icon");

    fireEvent.click(screen.getByTestId("ui-font-size-inc"));
    expect(input.value).toBe("14");
    // The choice is persisted so it survives a refresh.
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("14");
    // The discrete desktop size is applied live to the document root.
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe("14px");
  });

  it("disables the steppers at the min and max bounds", () => {
    localStorage.setItem("omnigent:ui-font-size", "18");
    renderPage("/settings/appearance");
    // At the 18px max, only the increase button is disabled.
    expect(screen.getByTestId("ui-font-size-inc")).toBeDisabled();
    expect(screen.getByTestId("ui-font-size-dec")).not.toBeDisabled();

    cleanup();
    localStorage.setItem("omnigent:ui-font-size", "11");
    renderPage("/settings/appearance");
    // At the 11px min, only the decrease button is disabled.
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

  it("resets every appearance preference back to product defaults", () => {
    localStorage.clear();
    renderPage("/settings/appearance");

    // Tweak a representative set of appearance preferences.
    mocks.theme = "dark";
    fireEvent.click(screen.getByTestId("theme-dark"));
    fireEvent.click(screen.getByTestId("terminal-theme-dark"));
    fireEvent.change(screen.getByTestId("color-theme-select") as HTMLSelectElement, {
      target: { value: "github" },
    });
    fireEvent.click(screen.getByTestId("transcript-view-default-terminal"));
    fireEvent.click(screen.getByTestId("workspace-panel-default-collapsed"));
    fireEvent.click(screen.getByTestId("hide-unconfigured-harnesses-toggle"));
    fireEvent.click(screen.getByTestId("ui-font-size-inc"));
    fireEvent.click(screen.getByTestId("ui-font-size-inc"));
    fireEvent.change(screen.getByTestId("ui-font-family-input") as HTMLInputElement, {
      target: { value: "Inter" },
    });
    fireEvent.click(screen.getByTestId("code-font-size-inc"));
    fireEvent.click(screen.getByTestId("code-font-size-inc"));
    fireEvent.change(screen.getByTestId("code-font-family-input") as HTMLInputElement, {
      target: { value: "Fira Code" },
    });
    fireEvent.click(screen.getByTestId("heavier-code-text-toggle"));

    // Sanity: the non-default choices were persisted.
    expect(localStorage.getItem("omnigent:terminal-theme")).toBe("dark");
    expect(localStorage.getItem("omnigent:default-transcript-view")).toBe("terminal");
    expect(localStorage.getItem("omnigent:ui-theme-palette")).toBe(JSON.stringify("github"));
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("15");
    expect(localStorage.getItem("omnigent:code-font-size")).toBe("15");
    expect(localStorage.getItem("omnigent:code-font-weight")).toBe("500");

    // Open the confirmation dialog and confirm the reset.
    fireEvent.click(screen.getByTestId("reset-appearance-button"));
    fireEvent.click(screen.getByTestId("reset-appearance-confirm"));

    // Mode is restored to "system".
    expect(mocks.setTheme).toHaveBeenCalledWith("system");

    // Fonts are back to their defaults.
    expect((screen.getByTestId("ui-font-size-input") as HTMLInputElement).value).toBe("13");
    expect((screen.getByTestId("ui-font-family-input") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("code-font-size-input") as HTMLInputElement).value).toBe("13");
    expect((screen.getByTestId("code-font-family-input") as HTMLInputElement).value).toBe("");
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe("13px");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBeNull();
    expect(localStorage.getItem("omnigent:code-font-size")).toBeNull();
    expect(localStorage.getItem("omnigent:code-font-weight")).toBeNull();

    // Color theme is back to Omnigent.
    expect((screen.getByTestId("color-theme-select") as HTMLSelectElement).value).toBe("omni");
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();

    // Terminal theme, transcript view, workspace panel, and harness visibility are restored.
    expect(screen.getByTestId("terminal-theme-auto")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("transcript-view-default-chat")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByTestId("workspace-panel-default-open")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByTestId("hide-unconfigured-harnesses-toggle")).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("lets you clear and retype the font size without clamping mid-edit", () => {
    localStorage.setItem("omnigent:ui-font-size", "13");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    expect(input.value).toBe("13");

    // Deleting a digit leaves "1" — below the 11px min. The box must SHOW "1"
    // (free editing) without snapping to 11 or persisting the transient value.
    fireEvent.change(input, { target: { value: "1" } });
    expect(input.value).toBe("1");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("13");
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe("");

    // Finishing the number to a valid size applies it live and persists it.
    fireEvent.change(input, { target: { value: "18" } });
    expect(input.value).toBe("18");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("18");
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe("18px");
  });

  it("clamps a below-min entry to the minimum on blur", () => {
    localStorage.setItem("omnigent:ui-font-size", "16");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.blur(input);
    // On blur the draft settles to the clamped minimum.
    expect(input.value).toBe("11");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("11");
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
    // No stored preference → 13px default, matching the interface default.
    expect(input.value).toBe("13");

    fireEvent.click(screen.getByTestId("code-font-size-inc"));
    expect(input.value).toBe("14");
    // Persisted under the code-font key (distinct from the chrome font's) so it
    // survives a refresh. It doesn't use --desktop-ui-font-size — the pref
    // reaches the editor/terminal imperatively, not via a CSS variable.
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

  it("shows and persists the code font weight", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const toggle = screen.getByTestId("heavier-code-text-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(localStorage.getItem("omnigent:code-font-weight")).toBe("500");
  });

  it("maps legacy font weights to the supported presets", () => {
    localStorage.setItem("omnigent:code-font-weight", "900");
    renderPage("/settings/appearance");
    expect(screen.getByTestId("heavier-code-text-toggle")).toHaveAttribute("aria-checked", "true");

    cleanup();
    localStorage.setItem("omnigent:code-font-weight", "100");
    renderPage("/settings/appearance");
    expect(screen.getByTestId("heavier-code-text-toggle")).toHaveAttribute("aria-checked", "false");
  });

  it("defaults bare /settings to General regardless of login mode", () => {
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "General" })).toBeInTheDocument();

    cleanup();
    mocks.accountsEnabled = false;
    mocks.loginUrl = null;
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "General" })).toBeInTheDocument();
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

    const unarchive = screen.getByTestId("unarchive-conversation");
    expect(unarchive).toHaveAccessibleName("Unarchive session");
    expect(within(unarchive).queryByText("Unarchive")).toBeNull();
    fireEvent.click(unarchive);
    expect(mocks.archiveMutate.mock.calls[0][0]).toEqual({
      id: "conv_archived",
      archived: false,
    });
    // Unarchiving opens the restored session (the mock mutate runs onSuccess).
    expect(screen.getByTestId("location").textContent).toBe("/c/conv_archived");
  });

  it("moves keyboard focus into the transcript when Return opens a row", async () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");
    const row = screen.getByTestId("archived-open-session");

    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });

    await waitFor(() => expect(screen.getByTestId("archive-transcript")).toHaveFocus());
  });

  it("debounces archive content search before issuing a new list query", async () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Searchable" })];
    renderPage("/settings/archived");
    fireEvent.click(screen.getByRole("button", { name: "Content" }));
    const input = screen.getByRole("searchbox", {
      name: "Search archived conversation content",
    });

    fireEvent.change(input, { target: { value: "b" } });
    fireEvent.change(input, { target: { value: "bounded" } });
    expect(mocks.archivedFilters?.searchQuery).toBe("");

    await waitFor(() => expect(mocks.archivedFilters?.searchQuery).toBe("bounded"), {
      timeout: 1_000,
    });
  });

  it("resizes the desktop archive list with the keyboard", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");
    const library = screen.getByTestId("archive-library");
    const list = library.firstElementChild as HTMLElement;
    const separator = screen.getByRole("separator", { name: "Resize archive session list" });

    expect(list).toHaveStyle({ width: "420px" });
    expect(separator).toHaveAttribute("aria-valuenow", "420");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(list).toHaveStyle({ width: "396px" });
    expect(separator).toHaveAttribute("aria-valuenow", "396");
  });

  it("renders the Archive Library outside the narrow shared settings scroller", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];

    renderPage("/settings/archived");

    const library = screen.getByTestId("archive-library");
    expect(library).toHaveClass("h-full", "min-w-0");
    expect(library.closest(".max-w-3xl")).toBeNull();
  });

  it("keeps All selected when one named facet coexists with unfiled rows", async () => {
    mocks.projectNames = ["Alpha"];
    mocks.conversations = [
      conv("filed", { archived: true, labels: { omni_project: "Alpha" } }),
      conv("unfiled", { archived: true }),
    ];

    renderPage("/settings/archived");

    await waitFor(() => expect(screen.getAllByTestId("archived-row")).toHaveLength(2));
    expect(mocks.archivedFilters?.project).toBeUndefined();
    expect(screen.getByRole("combobox", { name: /by project/i })).toHaveTextContent("All projects");
  });

  it("retains the saved expiration workflow using the stable archive time", () => {
    const now = 2_000_000_000_000;
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(now);
    localStorage.setItem("omnigent:archived-retention-days", "7");
    const archivedAt = Math.floor(now / 1000) - 8 * 86_400;
    mocks.conversations = [
      conv("expired", {
        archived: true,
        owner: "alice",
        updated_at: Math.floor(now / 1000),
        labels: { "omnigent.archived_at": String(archivedAt) },
      }),
      conv("shared-expired", {
        archived: true,
        owner: "bob",
        archived_at: archivedAt,
      }),
    ];

    renderPage("/settings/archived");

    expect(screen.getAllByText("Expired")).toHaveLength(2);
    expect(screen.getByText("1 expired on this page")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("delete-expired"));
    fireEvent.click(screen.getByRole("button", { name: "Delete 1 session" }));
    expect(mocks.bulkDeleteMutate).toHaveBeenCalledWith({ ids: ["expired"] });
    nowSpy.mockRestore();
  });

  it("keeps the compact archive toolbar available on mobile", () => {
    const restoreViewport = useMobileViewport();
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];

    try {
      renderPage("/settings/archived");

      expect(
        screen.getByRole("searchbox", { name: /Search archived session titles/ }),
      ).toBeVisible();
      expect(screen.getByRole("combobox", { name: /by project/i })).toBeVisible();
      expect(screen.getByRole("button", { name: /Archive sort/ })).toBeVisible();
      expect(screen.getByTestId("archived-row")).toBeInTheDocument();
      expect(screen.queryByTestId("archive-transcript")).toBeNull();
      expect(screen.getByTestId("archive-list-pane")).not.toHaveClass("hidden");

      fireEvent.click(screen.getByTestId("archived-open-session"));
      expect(screen.getByTestId("archive-transcript")).toHaveTextContent("Transcript: Old chat");
      expect(screen.getByTestId("archive-list-pane")).toHaveClass("hidden");
    } finally {
      cleanup();
      restoreViewport();
    }
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
    chooseArchiveFilter("Filter archived sessions by project", "Alpha");
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Alpha chat")).toBeInTheDocument();

    // Back to "All projects" restores the full list.
    chooseArchiveFilter("Filter archived sessions by project", "All projects");
    expect(screen.getAllByTestId("archived-row")).toHaveLength(2);
  });

  it("keeps the fuzzy project filter available when no archived session has a project", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    expect(screen.getByRole("combobox", { name: /by project/i })).toBeInTheDocument();
    expect(screen.getByTestId("archived-row")).toBeInTheDocument();
  });

  it("shows the empty state while keeping the reusable filters available", () => {
    mocks.conversations = [conv("conv_active")];
    renderPage("/settings/archived");

    expect(screen.getByText("No archived sessions match.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /by project/i })).toBeInTheDocument();
  });

  it("shows a project-scoped empty state when the picked project has no rows", () => {
    mocks.projectNames = ["Alpha"];
    mocks.conversations = [
      conv("conv_a", { archived: true, title: "Alpha chat", labels: { omni_project: "Alpha" } }),
    ];
    renderPage("/settings/archived");

    // Drop Alpha's only session so the filtered fetch returns nothing, then
    // pick Alpha (still an option because it's in the scanned name set).
    mocks.conversations = [];
    chooseArchiveFilter("Filter archived sessions by project", "Alpha");
    expect(screen.getByText("No archived sessions match.")).toBeInTheDocument();
  });

  it("offers archived-only projects whose sessions are beyond the first loaded page", () => {
    // The visible list's first page has no Gamma row, but the option scan
    // The aggregate facets endpoint found Gamma without loading its row.
    mocks.projectNames = ["Gamma"];
    mocks.conversations = [conv("p1", { archived: true, title: "Page-one chat" })];
    renderPage("/settings/archived");

    chooseArchiveFilter("Filter archived sessions by project", "Gamma");
    expect(screen.getByText("Gamma")).toBeInTheDocument();
  });

  it("treats a project literally named __all__ as a real project, not the clear-filter sentinel", () => {
    mocks.projectNames = ["Other", "__all__"];
    mocks.conversations = [
      conv("x1", { archived: true, title: "Edge chat", labels: { omni_project: "__all__" } }),
      conv("o1", { archived: true, title: "Other chat", labels: { omni_project: "Other" } }),
    ];
    renderPage("/settings/archived");

    // Picking the "__all__" project must FILTER to it (discriminated value
    // `project:__all__`), not clear the filter.
    chooseArchiveFilter("Filter archived sessions by project", "__all__");
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Edge chat")).toBeInTheDocument();
  });

  it("moves between bounded archive pages without accumulating prior rows", () => {
    mocks.pages = [
      [conv("a1", { archived: true, title: "First page chat" })],
      [conv("a2", { archived: true, title: "Second page chat" })],
    ];
    renderPage("/settings/archived");

    expect(
      screen.getByRole("heading", { name: "Archived sessions" }).closest("section"),
    ).toHaveClass("scroll-mt-16");
    expect(screen.getByText("First page chat")).toBeInTheDocument();
    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByTestId("archived-page-previous")).toBeDisabled();

    fireEvent.click(screen.getByTestId("archived-page-next"));
    expect(screen.queryByText("First page chat")).toBeNull();
    expect(screen.getByText("Second page chat")).toBeInTheDocument();
    expect(screen.getByText("Page 2")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("archived-page-previous"));
    expect(screen.getByText("First page chat")).toBeInTheDocument();
    expect(screen.queryByText("Second page chat")).toBeNull();
  });

  it("returns to page one when an archive filter changes", () => {
    mocks.projectNames = ["Alpha"];
    mocks.pages = [
      [conv("a1", { archived: true, title: "First page chat", labels: { omni_project: "Alpha" } })],
      [
        conv("a2", {
          archived: true,
          title: "Second page chat",
          labels: { omni_project: "Alpha" },
        }),
      ],
    ];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-page-next"));
    expect(screen.getByText("Page 2")).toBeInTheDocument();

    chooseArchiveFilter("Filter archived sessions by project", "Alpha");
    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByText("First page chat")).toBeInTheDocument();
  });

  it("keeps archive dates inline instead of adding visual group headers", () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Use local-time construction so bucket boundaries align with the
    // local-time arithmetic in dateGroupLabel regardless of the test runner's
    // timezone.
    vi.setSystemTime(new Date(2026, 6, 15, 12, 0, 0));

    try {
      const todaySec = new Date(2026, 6, 15, 10, 0, 0).getTime() / 1000;
      const yesterdaySec = new Date(2026, 6, 14, 8, 0, 0).getTime() / 1000;
      const fiveDaysAgoSec = new Date(2026, 6, 10, 8, 0, 0).getTime() / 1000;
      const twentyDaysAgoSec = new Date(2026, 5, 25, 8, 0, 0).getTime() / 1000;
      const oldDate = new Date(2026, 2, 1, 8, 0, 0);
      const oldSec = oldDate.getTime() / 1000;

      mocks.conversations = [
        conv("c_today", { archived: true, title: "Today chat", archived_at: todaySec }),
        conv("c_yesterday", { archived: true, title: "Yesterday chat", archived_at: yesterdaySec }),
        conv("c_week", { archived: true, title: "This week chat", archived_at: fiveDaysAgoSec }),
        conv("c_month", {
          archived: true,
          title: "This month chat",
          archived_at: twentyDaysAgoSec,
        }),
        conv("c_old", { archived: true, title: "Old chat", archived_at: oldSec }),
      ];
      renderPage("/settings/archived");

      expect(screen.getAllByTestId("archived-row")).toHaveLength(5);
      expect(screen.queryByRole("heading", { name: "Today" })).toBeNull();
      expect(screen.getAllByTestId("archived-row")[0].querySelectorAll("time")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("enters selection mode and selects rows via click", () => {
    mocks.conversations = [
      conv("a1", { archived: true, title: "Chat A" }),
      conv("a2", { archived: true, title: "Chat B" }),
    ];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-toggle-selection"));
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(2);

    // Clicking a row in selection mode toggles its checkbox.
    fireEvent.click(rows[0]);
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    fireEvent.click(rows[1]);
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    // Clicking again deselects.
    fireEvent.click(rows[0]);
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("bulk-deletes selected archived sessions after confirming", () => {
    mocks.conversations = [
      conv("a1", { archived: true, title: "Chat A" }),
      conv("a2", { archived: true, title: "Chat B" }),
    ];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-toggle-selection"));
    const rows = screen.getAllByTestId("archived-row");
    fireEvent.click(rows[0]);
    fireEvent.click(rows[1]);

    fireEvent.click(screen.getByTestId("archived-bulk-delete"));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(mocks.bulkDeleteMutate).toHaveBeenCalledWith({ ids: ["a1", "a2"] }, expect.anything());
  });

  it("bulk-unarchives selected archived sessions", () => {
    mocks.conversations = [
      conv("a1", { archived: true, title: "Chat A" }),
      conv("a2", { archived: true, title: "Chat B" }),
    ];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-toggle-selection"));
    fireEvent.click(screen.getAllByTestId("archived-row")[0]);

    const bulkUnarchive = screen.getByTestId("archived-bulk-unarchive");
    expect(bulkUnarchive).toHaveAccessibleName("Unarchive 1 selected session");
    expect(within(bulkUnarchive).queryByText(/Unarchive/)).toBeNull();
    fireEvent.click(bulkUnarchive);
    expect(mocks.bulkArchiveMutate).toHaveBeenCalledWith(
      { ids: ["a1"], archived: false },
      expect.anything(),
    );
  });

  it("exits selection mode and clears selection", () => {
    mocks.conversations = [conv("a1", { archived: true, title: "Chat A" })];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-toggle-selection"));
    fireEvent.click(screen.getAllByTestId("archived-row")[0]);
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("archived-exit-selection"));
    expect(screen.queryByText("1 selected")).toBeNull();
    expect(screen.queryByTestId("archived-bulk-delete")).toBeNull();
  });

  it("select-all picks every visible archived row", () => {
    mocks.conversations = [
      conv("a1", { archived: true, title: "Chat A" }),
      conv("a2", { archived: true, title: "Chat B" }),
      conv("a3", { archived: true, title: "Chat C" }),
    ];
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-toggle-selection"));
    fireEvent.click(screen.getByRole("button", { name: "Select visible" }));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
  });

  it("shows a compact title, project, host, agent, and archive date row", () => {
    mocks.hostIds = ["host-win"];
    mocks.agentNames = ["codex-native"];
    mocks.hosts = [{ host_id: "host-win", name: "Windows Workstation" }];
    mocks.conversations = [
      conv("a1", {
        archived: true,
        title: "Archive details",
        host_id: "host-win",
        agent_name: "codex-native",
        workspace: "D:/AIProgram/Projects/Omnigent",
        labels: { omni_project: "Omnigent" },
      }),
    ];

    renderPage("/settings/archived");

    expect(screen.queryByText("D:/AIProgram/Projects/Omnigent")).toBeNull();
    expect(screen.getByText("Omnigent")).toBeInTheDocument();
    const context = screen.getByTestId("archived-context");
    const row = screen.getByTestId("archived-row");
    expect(within(context).getByText("Windows Workstation")).toBeInTheDocument();
    expect(within(context).getByText("codex-native")).toBeInTheDocument();
    expect(row.querySelectorAll("time")).toHaveLength(1);
  });

  it("persists the compact archive view options", async () => {
    renderPage("/settings/archived");

    fireEvent.click(screen.getByRole("button", { name: "Content" }));

    await waitFor(() => {
      const stored = JSON.parse(
        localStorage.getItem("omnigent:archived-sessions-view-v1") ?? "{}",
      ) as Record<string, unknown>;
      expect(stored.searchScope).toBe("content");
      expect(mocks.archivedFilters?.searchScope).toBe("content");
    });
  });

  it("hides the Select button when there are no archived sessions", () => {
    mocks.conversations = [conv("conv_active")];
    renderPage("/settings/archived");

    expect(screen.queryByTestId("archived-toggle-selection")).toBeNull();
  });
});
