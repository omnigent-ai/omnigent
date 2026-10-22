// The new-session picker preselects the runner picked in desktop onboarding
// (resolved by useOnboardingRunnerHost), ahead of the remembered host.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({ data: undefined, isLoading: false }),
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));

import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useOnboardingRunnerHost } from "@/hooks/useOnboardingRunnerHost";
import { readLastHostChoice, writeLastHostChoice } from "@/lib/hostPreferences";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";

let searchParams = new URLSearchParams();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => vi.fn(),
  useSearchParams: () => [searchParams, vi.fn()],
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
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
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
vi.mock("@/hooks/useOnboardingRunnerHost", () => ({ useOnboardingRunnerHost: vi.fn() }));

function renderLanding() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<NewChatLandingScreen />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

const chip = () => screen.getByTestId("new-chat-landing-host-chip");

beforeEach(() => {
  vi.mocked(useHosts).mockReturnValue({
    data: [
      { host_id: "host_1", name: "remembered-box", owner: "me", status: "online" } as Host,
      { host_id: "host_2", name: "onboarding-box", owner: "me", status: "online" } as Host,
    ],
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
  // Each test is a fresh page after onboarding: no in-memory composer draft.
  resetLandingDraft();
  searchParams = new URLSearchParams();
  window.localStorage.clear();
  writeLastHostChoice("host_1");
});

afterEach(cleanup);

describe("NewChatLandingScreen onboarding runner", () => {
  it("selects the onboarding runner over the remembered host, and remembers it", async () => {
    vi.mocked(useOnboardingRunnerHost).mockReturnValue({ pending: false, hostId: "host_2" });
    renderLanding();
    await waitFor(() => expect(chip().getAttribute("aria-label")).toContain("onboarding-box"));
    expect(readLastHostChoice()).toBe("host_2");
  });

  it("applies the onboarding runner once, so a later pick survives a project switch", async () => {
    vi.mocked(useOnboardingRunnerHost).mockReturnValue({ pending: false, hostId: "host_2" });
    const { rerender } = renderLanding();
    await waitFor(() => expect(chip().getAttribute("aria-label")).toContain("onboarding-box"));
    fireEvent.pointerDown(chip(), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /remembered-box/ }));
    await waitFor(() => expect(chip().getAttribute("aria-label")).toContain("remembered-box"));
    // Another project's pencil changes the param in place, which clears the pick.
    searchParams = new URLSearchParams("project=Beta");
    rerender(<NewChatLandingScreen />);
    await waitFor(() => expect(chip().getAttribute("aria-label")).toContain("remembered-box"));
    expect(readLastHostChoice()).toBe("host_1");
  });

  it("keeps the remembered host when onboarding picked nothing", async () => {
    vi.mocked(useOnboardingRunnerHost).mockReturnValue({ pending: false, hostId: null });
    renderLanding();
    await waitFor(() => expect(chip().getAttribute("aria-label")).toContain("remembered-box"));
    expect(readLastHostChoice()).toBe("host_1");
  });

  it("holds the default while the onboarding runner is still coming online", async () => {
    vi.mocked(useOnboardingRunnerHost).mockReturnValue({ pending: true, hostId: null });
    renderLanding();
    // Once the landing screen has settled, the default-host effect must not have picked anything.
    await waitFor(() => expect(useOnboardingRunnerHost).toHaveBeenCalled());
    await act(async () => {});
    expect(chip().getAttribute("aria-label")).not.toContain("remembered-box");
    expect(chip().getAttribute("aria-label")).not.toContain("onboarding-box");
  });
});
