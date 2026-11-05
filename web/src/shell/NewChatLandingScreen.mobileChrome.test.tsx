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
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
// The landing screen's mobile chrome. One rule, about arriving at "/" on a
// phone: the composer is not focused, so landing here — including on the way
// back out of Settings — never throws up the keyboard unasked. (The iOS
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

vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ hash: "", state: null }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
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
  useProjects: () => ({ data: [], isLoading: false }),
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

function renderLanding(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<NewChatLandingScreen />, {
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
