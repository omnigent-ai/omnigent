// A `?prompt=` link must survive the landing composer being remounted or the
// page reloading right after the link is consumed, as an embedding host can do.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";
import type * as HostModule from "@/lib/host";
import type * as DictationInsertModule from "@/hooks/useDictationInsert";

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
  useOptionalLoadedConversations: () => undefined,
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));

import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useOnboardingRunnerHost } from "@/hooks/useOnboardingRunnerHost";
import { getCurrentUserId } from "@/lib/identity";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";

let searchParams = new URLSearchParams();
const replaceSearchParamsMock = vi.fn();
vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ hash: "", state: null }),
  useReplaceSearchParams: () => replaceSearchParamsMock,
  useNavigate: () => vi.fn(),
  useSearchParams: () => [searchParams, vi.fn()],
}));

let serverIdentity: string | null = "workspace:1";
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => serverIdentity,
}));

// Dictation writes the composer text without the textarea's onChange.
let dictationSetMessage: ((text: string) => void) | null = null;
vi.mock("@/hooks/useDictationInsert", async (importOriginal) => {
  const actual = await importOriginal<typeof DictationInsertModule>();
  return {
    ...actual,
    useDictationInsert: (...args: Parameters<typeof actual.useDictationInsert>) => {
      dictationSetMessage = args[1];
      return actual.useDictationInsert(...args);
    },
  };
});

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

function Landing({ visit }: { visit: number }) {
  return <NewChatLandingScreen key={visit} />;
}

function renderLanding() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<Landing visit={1} />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

const composer = () => screen.getByTestId("new-chat-landing-input");

/** A fresh document: the module-scoped draft is gone, sessionStorage is not. */
function reloadPage() {
  cleanup();
  resetLandingDraft();
  return renderLanding();
}

beforeEach(() => {
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "box", owner: "me", status: "online" } as Host],
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
  vi.mocked(useOnboardingRunnerHost).mockReturnValue({ pending: false, hostId: null });
  resetLandingDraft();
  window.localStorage.clear();
  window.sessionStorage.clear();
  serverIdentity = "workspace:1";
  vi.mocked(getCurrentUserId).mockReturnValue(null);
  searchParams = new URLSearchParams("o=123&prompt=hello_world");
  replaceSearchParamsMock.mockImplementation((next: URLSearchParams) => {
    searchParams = new URLSearchParams(next);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("embedded prompt link", () => {
  it("consumes the prompt from the URL", () => {
    renderLanding();
    expect(composer()).toHaveValue("hello_world");
    expect(replaceSearchParamsMock).toHaveBeenLastCalledWith(new URLSearchParams("o=123"));
  });

  it("keeps the prompt when the host remounts the composer in the same commit", () => {
    const { rerender } = renderLanding();
    expect(composer()).toHaveValue("hello_world");
    // The new instance renders before the old one's unmount cleanup parks the draft.
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("hello_world");
  });

  it("keeps the prompt when the page reloads after the URL was cleaned", () => {
    renderLanding();
    expect(searchParams.has("prompt")).toBe(false);
    reloadPage();
    expect(composer()).toHaveValue("hello_world");
  });

  it("drops the kept prompt once the user edits the composer", () => {
    renderLanding();
    fireEvent.change(composer(), { target: { value: "" } });
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("ignores a kept prompt once it is stale", () => {
    const now = Date.now();
    renderLanding();
    vi.spyOn(Date, "now").mockReturnValue(now + 5 * 60_000);
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("keeps the prompt over an older parked draft when the composer remounts", () => {
    searchParams = new URLSearchParams("o=123");
    const earlier = renderLanding();
    fireEvent.change(composer(), { target: { value: "Older draft" } });
    earlier.unmount();
    searchParams = new URLSearchParams("o=123&prompt=hello_world");
    const { rerender } = renderLanding();
    expect(composer()).toHaveValue("hello_world");
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("hello_world");
  });

  it("does not restore a prompt kept for another workspace", () => {
    renderLanding();
    serverIdentity = "workspace:2";
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("does not restore a prompt kept for another viewer", () => {
    vi.mocked(getCurrentUserId).mockReturnValue("alice");
    renderLanding();
    vi.mocked(getCurrentUserId).mockReturnValue("bob");
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("keeps nothing across a reload without a server identity", () => {
    serverIdentity = null;
    const { rerender } = renderLanding();
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("hello_world");
    reloadPage();
    expect(composer()).toHaveValue("");
    expect(window.sessionStorage.length).toBe(0);
  });

  it("drops the kept prompt when dictation changes the text", () => {
    renderLanding();
    act(() => dictationSetMessage?.("hello_world and more"));
    expect(composer()).toHaveValue("hello_world and more");
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("drops the kept prompt once a session is started", () => {
    renderLanding();
    fireEvent.keyDown(composer(), { key: "Enter" });
    expect(composer()).toHaveValue("hello_world");
    expect(window.sessionStorage.length).toBe(0);
    reloadPage();
    expect(composer()).toHaveValue("");
  });
});
