import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as ChatStoreModule from "@/store/chatStore";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as IdentityModule from "@/lib/identity";
import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";
import type * as UseConversationsModule from "@/hooks/useConversations";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Fixture adapted from NewChatDialog.hostSmartRouting.test.tsx, with Smart Routing off.
vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({ data: undefined }),
  useOptionalLoadedConversations: () => undefined,
}));
const skillsFixture = vi.hoisted(() => ({ skills: [] as { name: string; description: string }[] }));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({
    skills: skillsFixture.skills,
    skillsWarnings: [],
    skillsStatus: "ready",
    refetch: vi.fn(),
  }),
}));
vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelOptionsModule>()),
  useSandboxUnsupportedAgents: () => ({ unsupported: new Map(), prefetch: vi.fn() }),
  useSandboxModelOptions: () => ({
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
  }),
}));
// Keep transport helpers real; each test answers the requests through this mock.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(),
  fetchHosts: vi.fn(async () => []),
  useInstallHarness: () => ({ mutate: vi.fn(), isPending: false }),
  useInstallingHarnesses: () => new Set<string>(),
  useStoreCredential: () => ({ mutate: vi.fn(), isPending: false }),
  useDetectedCredentials: () => ({ data: [] }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
  USER_AGENTS_QUERY_KEY: ["available-agents-user"],
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined, isLoading: false, error: null }),
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
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  useHarnessSetupSteps: () => ({}),
}));
vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  setPendingInitialPrompt: vi.fn(),
}));

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { testAgent } from "@/test/agentFixtures";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";

const SERVER_INFO = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: { harness_install: false, ug_smart_routing_ui: false },
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: false,
} as unknown as ServerInfo;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

function renderLanding(harness: string) {
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [testAgent("a1", `${harness}-ui`, { display_name: harness, harness })],
    isSuccess: true,
  } as unknown as ReturnType<typeof useAvailableAgents>);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider info={SERVER_INFO}>
        <TooltipProvider>
          <MemoryRouter>
            <NewChatLandingScreen />
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

const input = () => screen.getByTestId("new-chat-landing-input");
const type = (value: string) => fireEvent.change(input(), { target: { value } });

beforeEach(() => {
  skillsFixture.skills = [{ name: "team", description: "Run a team" }];
  resetLandingDraft();
  localStorage.clear();
  localStorage.setItem("omnigent:recent-workspaces", JSON.stringify({ host_1: ["/work/repo"] }));
  vi.mocked(authenticatedFetch)
    .mockReset()
    .mockImplementation(async (url) =>
      jsonResponse(String(url) === "/v1/sessions" ? { id: "conv_new" } : {}),
    );
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "machine-1", owner: "me", status: "online" }],
    isSuccess: true,
  } as unknown as ReturnType<typeof useHosts>);
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  vi.mocked(setPendingInitialPrompt).mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("landing composer highlight", () => {
  it("tints a known skill but not an unknown command", () => {
    renderLanding("claude-sdk");
    type("/team fix the bug");
    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay).toHaveTextContent("/team fix the bug");
    expect(within(overlay).getByText("/team")).toHaveClass("text-brand-accent");
    expect(input()).toHaveAttribute("data-slash-command", "true");

    type("/typo fix the bug");
    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
    expect(input()).not.toHaveAttribute("data-slash-command");
  });

  it("marks a claude-native `!` draft as a shell command and sends it unchanged", async () => {
    renderLanding("claude-native");
    type("!git status --short");
    expect(input()).toHaveAttribute("data-shell-command", "true");
    expect(input()).toHaveAccessibleDescription("Shell command");
    expect(screen.getByTestId("composer-shell-mode-indicator")).toBeVisible();

    const submit = screen.getByTestId("new-chat-landing-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() =>
      expect(setPendingInitialPrompt).toHaveBeenCalledWith(
        "conv_new",
        expect.objectContaining({ text: "!git status --short" }),
      ),
    );
  });

  it("does not offer shell mode on a harness without native `!` input", () => {
    renderLanding("claude-sdk");
    type("!git status");
    expect(screen.queryByTestId("composer-shell-mode-indicator")).toBeNull();
    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
  });
});
