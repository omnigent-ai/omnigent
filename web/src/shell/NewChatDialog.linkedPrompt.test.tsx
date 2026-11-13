// A `?prompt=` link must survive the landing composer being remounted or the
// page reloading right after the link is consumed, as an embedding host can do.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";
import type * as HostModule from "@/lib/host";
import type * as SessionsApiModule from "@/lib/sessionsApi";
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

let loadedConversations: unknown = undefined;
vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({ data: loadedConversations, isLoading: false }),
  useOptionalLoadedConversations: () => loadedConversations,
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
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { TooltipProvider } from "@/components/ui/tooltip";
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { setPendingInitialPrompt } from "@/store/chatStore";
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

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(async (id: string) => ({ id, hostId: "host_1", workspace: "" })),
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

function Landing({ visit }: { visit: number }) {
  return <NewChatLandingScreen key={visit} />;
}

const serverInfo: Partial<ServerInfo> = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  enabled_connections: [],
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: {},
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: false,
  sibling_sessions: true,
};

function renderLanding() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<Landing visit={1} />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={serverInfo as ServerInfo}>
          <TooltipProvider>{children}</TooltipProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
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
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(setPendingInitialPrompt).mockReset();
  loadedConversations = undefined;
  searchParams = new URLSearchParams("o=123&prompt=hello_world");
  replaceSearchParamsMock.mockImplementation((next: URLSearchParams) => {
    searchParams = new URLSearchParams(next);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** Make a create possible: a claude-sdk agent on an online host with a recent workspace. */
function allowSessionStart(response: { ok: boolean; body: unknown }) {
  window.localStorage.setItem(
    "omnigent:recent-workspaces",
    JSON.stringify({ host_1: ["/work/repo"] }),
  );
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "ag_hello",
        name: "hello_world",
        display_name: "Hello World",
        description: null,
        harness: "claude-sdk",
        skills: [],
      } as AvailableAgent,
    ],
  } as ReturnType<typeof useAvailableAgents>);
  vi.mocked(authenticatedFetch).mockResolvedValue({
    ok: response.ok,
    status: response.ok ? 200 : 500,
    json: async () => response.body,
  } as unknown as Response);
}

/** Hold the session-create POST open and return a function that completes it. */
function deferCreate(): (response: Response) => void {
  let finish: (response: Response) => void = () => {};
  vi.mocked(authenticatedFetch).mockImplementation((url, init) =>
    url === "/v1/sessions" && init?.method === "POST"
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : Promise.resolve({ ok: true, json: async () => ({}) } as unknown as Response),
  );
  return (response) => finish(response);
}

async function submitWhenReady() {
  await waitFor(() => expect(screen.getByTestId("new-chat-landing-submit")).not.toBeDisabled());
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  await waitFor(() =>
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.some(([url, init]) => url === "/v1/sessions" && init?.method === "POST"),
    ).toBe(true),
  );
}

describe("embedded prompt link", () => {
  it("keeps the prompt when the host remounts the composer in the same commit", () => {
    const { rerender } = renderLanding();
    expect(composer()).toHaveValue("hello_world");
    expect(replaceSearchParamsMock).toHaveBeenLastCalledWith(new URLSearchParams("o=123"));
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

  it.each([
    [59_999, "hello_world"],
    [60_000, ""],
  ])("restores a kept prompt only within 60 s (%i ms)", (elapsed, expected) => {
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    renderLanding();
    vi.spyOn(Date, "now").mockReturnValue(now + elapsed);
    reloadPage();
    expect(composer()).toHaveValue(expected);
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

  it.each([
    ["alice", "bob", ""],
    ["alice", "alice", "hello_world"],
    // Identity resolves after first render, so an unknown viewer on either side
    // is not tied to the prompt.
    [null, "bob", "hello_world"],
    ["alice", null, "hello_world"],
  ])("restores a prompt kept for viewer %s when %s reads it", (keptBy, readBy, expected) => {
    vi.mocked(getCurrentUserId).mockReturnValue(keptBy);
    renderLanding();
    vi.mocked(getCurrentUserId).mockReturnValue(readBy);
    reloadPage();
    expect(composer()).toHaveValue(expected);
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

  it("keeps the prompt when a blocked submit starts no session", () => {
    renderLanding();
    fireEvent.keyDown(composer(), { key: "Enter" });
    expect(composer()).toHaveValue("hello_world");
    expect(authenticatedFetch).not.toHaveBeenCalledWith("/v1/sessions", expect.anything());
    reloadPage();
    expect(composer()).toHaveValue("hello_world");
  });

  it("drops the kept prompt once a session is created", async () => {
    allowSessionStart({ ok: true, body: { id: "conv_new" } });
    renderLanding();
    await submitWhenReady();
    await waitFor(() => expect(window.sessionStorage.length).toBe(0));
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("keeps the prompt when the create fails", async () => {
    allowSessionStart({ ok: false, body: { error: { message: "Create failed" } } });
    renderLanding();
    await submitWhenReady();
    expect(await screen.findByText(/Create failed/)).toBeVisible();
    expect(composer()).toHaveValue("hello_world");
    reloadPage();
    expect(composer()).toHaveValue("hello_world");
  });

  it.each([
    ["a remounted composer", true],
    ["the same composer", false],
  ])("does not let an older create forget a newer prompt in %s", async (_case, remount) => {
    allowSessionStart({ ok: true, body: { id: "conv_a" } });
    const finishCreate = deferCreate();
    const { rerender } = renderLanding();
    await submitWhenReady();
    searchParams = new URLSearchParams("o=123&prompt=Second+request");
    if (remount) {
      cleanup();
      renderLanding();
    } else {
      rerender(<Landing visit={1} />);
    }
    expect(composer()).toHaveValue("Second request");
    await act(async () => {
      finishCreate({ ok: true, json: async () => ({ id: "conv_a" }) } as unknown as Response);
    });
    reloadPage();
    expect(composer()).toHaveValue("Second request");
  });

  it("does not restore a submitted prompt into a same-commit remount while creating", async () => {
    allowSessionStart({ ok: true, body: { id: "conv_a" } });
    const finishCreate = deferCreate();
    const { rerender } = renderLanding();
    await submitWhenReady();
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("");
    await act(async () => {
      finishCreate({ ok: true, json: async () => ({ id: "conv_a" }) } as unknown as Response);
    });
    expect(composer()).toHaveValue("");
  });

  it("sends the link's text literally after a mounted link and a remount", async () => {
    allowSessionStart({ ok: true, body: { id: "conv_new" } });
    loadedConversations = {
      pages: [{ data: [{ id: "s_parked", object: "conversation", title: "Plan", labels: {} }] }],
    };
    searchParams = new URLSearchParams("o=123");
    const { rerender } = renderLanding();
    fireEvent.change(composer(), { target: { value: "Read @pla", selectionStart: 9 } });
    fireEvent.click(screen.getByTitle("Tag session Plan"));
    expect(screen.getByTestId("composer-mention-token").textContent).toBe("@Plan");
    searchParams = new URLSearchParams("o=123&prompt=Ask+%40Plan");
    rerender(<Landing visit={1} />);
    expect(composer()).toHaveValue("Ask @Plan");
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("Ask @Plan");
    await submitWhenReady();
    await waitFor(() => expect(setPendingInitialPrompt).toHaveBeenCalled());
    expect((vi.mocked(setPendingInitialPrompt).mock.calls[0]![1] as { text: string }).text).toBe(
      "Ask @Plan",
    );
  });

  it("drops the kept prompt when the text changes after a remount", () => {
    const { rerender } = renderLanding();
    rerender(<Landing visit={2} />);
    expect(composer()).toHaveValue("hello_world");
    fireEvent.change(composer(), { target: { value: "Edited after remount" } });
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it.each([
    ["a nested-agent composer", "o=123&prompt=hello_world", "sibling=source"],
    ["the standalone composer", "sibling=source&prompt=hello_world", "o=123"],
  ])("does not show the prompt in %s", async (_target, linked, next) => {
    searchParams = new URLSearchParams(linked);
    renderLanding();
    expect(await screen.findByTestId("new-chat-landing-input")).toHaveValue("hello_world");
    cleanup();
    searchParams = new URLSearchParams(next);
    renderLanding();
    expect(await screen.findByTestId("new-chat-landing-input")).toHaveValue("");
  });

  it("forgets the kept prompt when a handoff replaces the draft", () => {
    renderLanding();
    cleanup();
    searchParams = new URLSearchParams(newSessionLocation("Handed off", []).split("?")[1]);
    renderLanding();
    expect(composer()).toHaveValue("Handed off");
    expect(window.sessionStorage.length).toBe(0);
    reloadPage();
    expect(composer()).toHaveValue("");
  });

  it("forgets an older kept prompt when storing its replacement fails", () => {
    renderLanding();
    cleanup();
    searchParams = new URLSearchParams("o=123&prompt=Second+request");
    const quota = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    renderLanding();
    expect(composer()).toHaveValue("Second request");
    quota.mockRestore();
    fireEvent.change(composer(), { target: { value: "" } });
    reloadPage();
    expect(composer()).toHaveValue("");
  });
});
