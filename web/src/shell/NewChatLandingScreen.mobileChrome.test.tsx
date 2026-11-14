import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";

vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelOptionsModule>()),
  useSandboxModelOptions: vi.fn(() => ({
    data: {
      configured: false,
      status: "unconfigured",
      models: [],
      configuration_revision: null,
      provider_label: null,
      default_model: null,
    },
    isLoading: false,
    error: null,
  })),
}));
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({ data: undefined, isLoading: false }),
  useOptionalLoadedConversations: () => undefined,
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
// The landing screen's mobile chrome. Arriving at "/" on a phone does not
// focus the composer, so landing here — including on the way back out of
// Settings — never throws up the keyboard unasked. The project chip under the
// heading switches the visit's `?project=` without a search field. (The iOS
// shell's legacy bottom Chat/Terminal bar is kept hidden app-wide at boot by
// hideNativeChatTerminalBar in main.tsx, not per-screen here.)

import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";

import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { NewChatLandingScreen } from "./NewChatDialog";
import { MobileHeaderButtonGroup } from "./MobileHeaderButton";
import { MobileHeaderControlsProvider, MobileHeaderControlsSlot } from "./MobileHeaderControlsSlot";

const replaceSearchParamsMock = vi.fn();
const route = vi.hoisted(() => ({
  navigate: vi.fn(),
  params: new URLSearchParams(),
  projects: [] as { id: string; name: string; icon?: string | null }[],
}));
vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ hash: "", state: null }),
  useReplaceSearchParams: () => replaceSearchParamsMock,
  useNavigate: () => route.navigate,
  useSearchParams: () => [route.params, vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({ setPendingInitialPrompt: vi.fn() }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: [] })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", async (importOriginal) => ({
  ...(await importOriginal<typeof HostWorktreesModule>()),
  useHostWorktrees: () => ({ data: [], isError: false }),
  hostWorktreesQueryOptions: (hostId: string, repoPath: string) => ({
    queryKey: ["host-worktrees", hostId, repoPath],
    queryFn: async () => [],
  }),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({ useDirectorySessions: () => ({ data: [] }) }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: () => ({ data: route.projects, isLoading: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  useHarnessSetupSteps: () => ({}),
}));
/** Point `matchMedia` at a phone-width (or desktop-width) viewport. */
function setViewport(mobile: boolean): void {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: mobile && query.includes("max-width"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

function renderLanding() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<NewChatLandingScreen />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={{ ...SERVER_INFO_OFFLINE_FALLBACK }}>
          <MobileHeaderControlsProvider>
            <MobileHeaderButtonGroup>
              <MobileHeaderControlsSlot sessionId={null} />
            </MobileHeaderButtonGroup>
            {children}
          </MobileHeaderControlsProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    ),
  });
}

beforeEach(() => {
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "laptop", owner: "corey", status: "online" } as Host],
  } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "ag_hello",
        name: "hello_world",
        display_name: "Hello World",
        description: null,
        harness: null,
        skills: [],
      } as AvailableAgent,
    ],
  } as ReturnType<typeof useAvailableAgents>);
  setViewport(false);
  route.navigate.mockReset();
  route.params = new URLSearchParams();
  route.projects = [];
});

afterEach(() => {
  cleanup();
  setViewport(false);
});

describe("NewChatLandingScreen keyboard behaviour", () => {
  it("focuses the composer on desktop so typing can start immediately", async () => {
    renderLanding();

    expect(document.activeElement).toBe(screen.getByTestId("new-chat-landing-input"));
    const composer = screen.getByTestId("new-chat-landing-composer");
    expect(composer).toContainElement(screen.getByTestId("new-chat-landing-host-chip"));
    expect(composer).toContainElement(await screen.findByTestId("new-chat-landing-agent-select"));
  });

  it("keeps mobile launch controls in the header without focusing or losing the draft", async () => {
    setViewport(true);
    renderLanding();

    expect(document.activeElement).not.toBe(screen.getByTestId("new-chat-landing-input"));
    const header = screen.getByTestId("mobile-header-controls-slot");
    expect(header).toHaveClass("no-drag", "pointer-events-auto");
    const host = screen.getByTestId("new-chat-landing-host-chip");
    const harness = await screen.findByTestId("new-chat-landing-agent-select");
    const composer = screen.getByTestId("new-chat-landing-composer");
    expect(composer).toHaveAttribute("data-mobile-composer", "collapsed");
    fireEvent.focus(host);
    expect(composer).toHaveAttribute("data-mobile-composer", "collapsed");
    expect(header).toContainElement(host);
    expect(header).toContainElement(harness);
    expect(screen.queryByTestId("new-chat-landing-actions-menu")).not.toBeInTheDocument();
    expect(screen.getByTestId("new-chat-landing-composer")).not.toContainElement(host);
    const workspace = screen.getByTestId("new-chat-landing-workspace-chip");
    expect(header).not.toContainElement(workspace);
    expect(screen.getByTestId("new-chat-landing-composer-surface")).not.toContainElement(workspace);

    const add = screen.getByRole("button", { name: "Add" });
    fireEvent.focus(add);
    expect(composer).toHaveAttribute("data-mobile-composer", "collapsed");
    fireEvent.pointerDown(add, { button: 0, ctrlKey: false, pointerType: "mouse" });
    const addMenu = await screen.findByTestId("new-chat-landing-add-menu");
    expect(addMenu).toBeVisible();
    expect(composer).toHaveAttribute("data-mobile-composer", "collapsed");
    fireEvent.keyDown(addMenu, { key: "Escape" });
    await waitFor(() => expect(add).toHaveAttribute("aria-expanded", "false"));
    expect(composer).toHaveAttribute("data-mobile-composer", "collapsed");

    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.focus(input);
    expect(composer).toHaveAttribute("data-mobile-composer", "expanded");
    fireEvent.change(input, { target: { value: "Keep this draft" } });
    fireEvent.pointerDown(host, { button: 0, ctrlKey: false, pointerType: "mouse" });
    const hostMenu = await screen.findByTestId("new-chat-landing-host-menu");
    expect(within(hostMenu).getByTestId("new-chat-landing-host-host_1")).toHaveTextContent(
      "laptop",
    );
    fireEvent.keyDown(hostMenu, { key: "Escape" });
    await waitFor(() => expect(host).toHaveAttribute("aria-expanded", "false"));

    fireEvent.pointerDown(harness, { button: 0, ctrlKey: false, pointerType: "mouse" });
    const harnessMenu = await screen.findByRole("menu");
    expect(within(harnessMenu).getByText("Agents")).toBeVisible();
    fireEvent.keyDown(harnessMenu, { key: "Escape" });
    await waitFor(() => expect(harness).toHaveAttribute("aria-expanded", "false"));

    fireEvent.click(workspace);
    expect(await screen.findByTestId("new-chat-landing-workspace-open-folder")).toBeVisible();
    expect(input).toHaveValue("Keep this draft");
  });
});

describe("NewChatLandingScreen project chip", () => {
  async function openProjectMenu(): Promise<HTMLElement> {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-project-chip"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    return screen.findByTestId("new-chat-landing-project-menu");
  }

  beforeEach(() => {
    route.projects = [
      { id: "proj_web", name: "web", icon: "🌐" },
      { id: "proj_api", name: "api" },
    ];
  });

  it("is not rendered on desktop, which names the project in its heading", () => {
    route.params = new URLSearchParams("project=web");
    renderLanding();
    expect(screen.getByRole("heading", { name: "web" })).toBeInTheDocument();
    expect(screen.queryByTestId("new-chat-landing-project-chip")).not.toBeInTheDocument();
  });

  it("leads the phone controls and files the visit under a picked project", async () => {
    setViewport(true);
    route.params = new URLSearchParams("ref=docs");
    renderLanding();

    const chip = screen.getByTestId("new-chat-landing-project-chip");
    expect(chip).toHaveAccessibleName("Project: No project");
    const controls = screen.getByTestId("new-chat-landing-workspace-controls");
    expect(controls.firstElementChild).toContainElement(chip);

    const menu = await openProjectMenu();
    expect(within(menu).queryByRole("textbox")).not.toBeInTheDocument();
    const rows = within(menu).getAllByRole("menuitemradio");
    expect(rows.map((row) => row.textContent)).toEqual(["No project", "🌐web", "api"]);
    expect(rows.map((row) => row.getAttribute("aria-checked"))).toEqual(["true", "false", "false"]);
    for (const row of rows) expect(row).toHaveClass("min-h-11");
    fireEvent.click(within(menu).getByRole("menuitemradio", { name: /api/ }));
    expect(route.navigate).toHaveBeenCalledWith("/?ref=docs&project=api");
  });

  it("names the current project and clears only `project` for No project", async () => {
    setViewport(true);
    route.params = new URLSearchParams("project=web&ref=docs");
    const { rerender } = renderLanding();

    const chip = screen.getByTestId("new-chat-landing-project-chip");
    expect(chip).toHaveAccessibleName("Project: web");
    expect(within(chip).getByTestId("project-icon")).toHaveTextContent("🌐");

    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.change(input, { target: { value: "Keep this draft" } });
    const menu = await openProjectMenu();
    expect(within(menu).getByRole("menuitemradio", { name: /web/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    const noProject = within(menu).getByRole("menuitemradio", { name: "No project" });
    expect(noProject).toHaveAttribute("aria-checked", "false");
    fireEvent.click(noProject);
    expect(route.navigate).toHaveBeenCalledWith("/?ref=docs");

    // Apply the navigation: the landing stays mounted and reseeds as a plain visit.
    route.params = new URLSearchParams("ref=docs");
    rerender(<NewChatLandingScreen />);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-project-chip")).toHaveAccessibleName(
        "Project: No project",
      ),
    );
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Keep this draft");
    expect(document.activeElement).not.toBe(screen.getByTestId("new-chat-landing-input"));
  });
});
