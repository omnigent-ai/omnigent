import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type { SessionListWireItem } from "@/lib/sessionListCache";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { clearOptimisticTitles, getOptimisticTitle } from "@/lib/optimisticTitles";
import type { Host } from "@/hooks/useHosts";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { NewChatLandingScreen, resetLandingDraft, sanitizeInitialPrompt } from "./NewChatDialog";
import { writeDefaultBaseBranch } from "@/lib/baseBranchPreferences";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { landingStorageKey } from "@/lib/landingStorage";

// The landing screen drives the real Web-start flow end to end: the host and
// first agent auto-select, the working directory seeds from the host's most-
// recent path, the composer message is the first prompt, and hitting send
// POSTs /v1/sessions then navigates. The branches under test are the request
// body the screen builds (host_id + workspace + agent_id), the terminal-
// wrapper labels for the claude-native agent, the permission-mode
// terminal_launch_args, the git worktree fields, and the sanitized prompt
// handoff. The host list, agent catalog, conflict hooks, navigation and HTTP
// layers are stubbed so the test isolates that wiring.
const navigateMock = vi.fn();
const setPendingInitialPromptMock = vi.fn();
const beginLocalConversationMock = vi.fn();
const hydrateLocalConversationMock = vi.fn();
const removeLocalConversationMock = vi.fn();
const identityState = vi.hoisted(() => ({
  userId: null as string | null,
  resolveIdentity: () => Promise.resolve(null as string | null),
}));
let searchParams = new URLSearchParams();
let projects: { id: string | null; name: string }[] = [];
let projectConfigs: Record<string, { host_id?: string; workspace?: string; agent_id?: string }> =
  {};

const RECENT_KEY = "omnigent:recent-workspaces";
// Prompt history is scoped per conversation; the landing composer writes under
// the newly created session id (``conv_new`` in these tests), so the recall
// stack lives at the prefixed key, not the bare one.
const PROMPT_HISTORY_KEY = "omnigent:prompt-history:conv_new";
// The seeded working directory (from the host's persisted recent) that the
// create body must carry through.
const SEEDED_WORKSPACE = "/Users/corey/universe/src/foo";

// The landing screen navigates via the embed-aware routing abstraction
// (`@/lib/routing`), not react-router directly — mock that so the create
// flow's navigate() lands on our spy regardless of router/provider setup.
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigateMock,
  useSearchParams: () => [searchParams, vi.fn()],
}));

// The screen hands the first message to ChatPage through the chatStore
// (keyed by conversation id), not router state — assert on that call.
vi.mock("@/store/chatStore", () => ({
  beginLocalConversation: (...args: unknown[]) => beginLocalConversationMock(...args),
  hydrateLocalConversation: (...args: unknown[]) => hydrateLocalConversationMock(...args),
  removeLocalConversation: (...args: unknown[]) => removeLocalConversationMock(...args),
  setPendingInitialPrompt: (...args: unknown[]) => setPendingInitialPromptMock(...args),
}));

// The create races the POST against the updates stream's announcement of the
// new session row. Stub the stream so a test decides when (and with what row)
// that announcement lands, and can inspect the matcher the screen built to
// tell its own session apart from everything else the stream pushes.
const pushMatchers: ((item: SessionListWireItem) => boolean)[] = [];
let announcePushedSession: ((row: SessionListWireItem | null) => void) | null = null;
vi.mock("@/lib/sessionUpdatesSocket", () => ({
  nextPushedSession: (match: (item: SessionListWireItem) => boolean, signal: AbortSignal) => {
    pushMatchers.push(match);
    return new Promise<SessionListWireItem | null>((resolve) => {
      announcePushedSession = resolve;
      signal.addEventListener("abort", () => resolve(null), { once: true });
    });
  },
  // Inert transport for anything else in the tree that observes connectivity.
  sessionUpdatesSocket: {
    subscribeStatus: () => () => {},
    isConnected: () => true,
  },
}));

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: () => identityState.userId,
  resolveIdentity: () => identityState.resolveIdentity(),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({
    data: [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
      { id: "haiku", displayName: "Haiku" },
    ],
  })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
// The home listing is only consulted when there's no recent; the recent is
// always set here, so keep this inert (returns no listing).
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  // WorkspacePicker reads this on mount when the file browser opens;
  // an idle mutation keeps it inert for these tests.
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: () => ({ data: undefined }),
}));
// No other sessions in scope — keep the conflict hooks inert so they don't
// issue their own /health fetch or surface a warning. The warning is covered
// in NewChatDialog.test.tsx.
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: () => ({ data: projects }),
  useProjectConfig: (id: string | null) => ({
    data: id === null ? null : (projectConfigs[id] ?? null),
    isLoading: false,
  }),
  // Same reason as useProjects above: the landing reads useConversations for
  // hasNoSessions, so stub it to avoid an authenticatedFetch skewing calls[0].
  useConversations: () => ({ data: undefined }),
}));
// Dynamic harness-label fetching is covered separately. Keep it synchronous
// here so exact create-POST call-count assertions only observe the POST.
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
  // Stub so the setup dialog's hook doesn't fire its own /v1/harnesses fetch
  // (which would skew the create-flow call-count assertions here).
  useHarnessSetupSteps: () => ({}),
}));

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "corey-laptop",
    owner: "corey",
    status: "online",
    ...overrides,
  };
}

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_hello",
    name: "hello_world",
    display_name: "Hello World",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

function setHosts(hosts: Host[]): void {
  vi.mocked(useHosts).mockReturnValue({ data: hosts } as ReturnType<typeof useHosts>);
}

function setAgents(agents: AvailableAgent[]): void {
  vi.mocked(useAvailableAgents).mockReturnValue({ data: agents } as ReturnType<
    typeof useAvailableAgents
  >);
}

function renderLanding(cachedSessionIds: string[] = [], infoOverrides: Partial<ServerInfo> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  if (cachedSessionIds.length > 0) {
    // Sessions already on screen when the create starts. "Never seen by this
    // tab" is part of how the screen recognizes its own announced row, so the
    // case that covers that seeds the cache the check reads.
    client.setQueryData(["conversations", "", false], {
      pages: [
        {
          data: cachedSessionIds.map((id) => ({ id })),
          first_id: cachedSessionIds[0],
          last_id: cachedSessionIds.at(-1),
          has_more: false,
        },
      ],
      pageParams: [undefined],
    });
  }
  const info = {
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
    ...infoOverrides,
  } as ServerInfo;
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  return render(<NewChatLandingScreen />, { wrapper: Wrapper });
}

/**
 * Type the composer message that doubles as the first prompt. Submit is
 * disabled until this is non-empty, so every create path needs it.
 */
function typeMessage(text: string): void {
  fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
    target: { value: text },
  });
}

/** Wait for the working directory to seed from the recent before submitting. */
async function waitForWorkspaceSeed(): Promise<void> {
  // The chip shows the basename ("foo") once the seed effect runs.
  await waitFor(() =>
    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
  );
}

/** Open the git-worktree popover so its branch fields mount. */
function openWorktree(): void {
  fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
}

/**
 * Open the picker and commit (select + close) an agent by clicking its row.
 * Only the fully supported harnesses lead inline; the rest sit under "More", so
 * drill in when the row isn't already listed.
 */
function selectAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
}

/**
 * Select <agentId> and open its run-config modal via the composer gear icon.
 * The knobs (model / effort / permission / approval / cursor mode / brain
 * harness) live in this modal, not the picker dropdown.
 */
function openAgentModels(agentId: string): void {
  const picker = screen.getByTestId("new-chat-landing-agent-select");
  fireEvent.pointerDown(picker, { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
  }
  if (screen.queryByTestId(`new-chat-landing-agent-config-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
    fireEvent.pointerDown(picker, { button: 0 });
    if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
      fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
    }
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-config-${agentId}`));
}

function openAgentConfig(agentId: string): void {
  openAgentModels(agentId);
  fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
}

/** Open a Radix Select trigger (opens on pointerdown in jsdom). */
function openSelect(testId: string): void {
  fireEvent.pointerDown(screen.getByTestId(testId), { button: 0 });
  fireEvent.click(screen.getByTestId(testId));
}

/** Open the config-modal Select at <triggerTestId> and click the option labeled <label>. */
function pickSelectOption(triggerTestId: string, label: string): void {
  openSelect(triggerTestId);
  fireEvent.click(screen.getByText(label));
}

/** Close the config modal by clicking Save (commits the draft). */
function saveConfig(): void {
  fireEvent.click(screen.getByTestId("new-chat-landing-config-save"));
}

beforeEach(() => {
  navigateMock.mockReset();
  setPendingInitialPromptMock.mockReset();
  beginLocalConversationMock.mockReset();
  beginLocalConversationMock.mockReturnValue(null);
  hydrateLocalConversationMock.mockReset();
  removeLocalConversationMock.mockReset();
  removeLocalConversationMock.mockReturnValue(false);
  pushMatchers.length = 0;
  announcePushedSession = null;
  identityState.userId = null;
  identityState.resolveIdentity = () => Promise.resolve(identityState.userId);
  vi.mocked(authenticatedFetch).mockReset();
  // Clear the module-level landing draft so a base branch (or other field)
  // left behind by an unmounting test doesn't seed the next one.
  resetLandingDraft();
  clearOptimisticTitles();
  localStorage.clear();
  searchParams = new URLSearchParams();
  projects = [];
  projectConfigs = {};
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
      { id: "haiku", displayName: "Haiku" },
    ],
    isLoading: false,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  // Seed host_1's recent so the working directory pre-fills deterministically
  // (the create body must carry SEEDED_WORKSPACE through).
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [SEEDED_WORKSPACE] }));
  setHosts([host()]);
  setAgents([agent()]);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen create flow", () => {
  it("keeps project placement on the provisional and rekeyed conversation", async () => {
    searchParams = new URLSearchParams("project=Alpha");
    projects = [{ id: "proj_alpha", name: "Alpha" }];
    beginLocalConversationMock.mockReturnValue({
      tempConvId: "temp:1234567890abcdef1234567890abcdef",
      pendingMsgTempId: "pend_1",
      createToken: "1234567890abcdef1234567890abcdef",
    });
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    const project = { id: "proj_alpha", name: "Alpha" };
    await waitFor(() =>
      expect(beginLocalConversationMock).toHaveBeenCalledWith(
        "inspect the repo",
        [],
        expect.any(Object),
        project,
        expect.objectContaining({ boundAgentId: "ag_hello", reasoningEffort: null }),
      ),
    );
    await waitFor(() =>
      expect(hydrateLocalConversationMock).toHaveBeenCalledWith(
        "temp:1234567890abcdef1234567890abcdef",
        "conv_new",
        "ag_hello",
        "inspect the repo",
        [],
        "pend_1",
        null,
        navigateMock,
        expect.any(Function),
        project,
      ),
    );
  });

  it("waits for pending workspace tools before Start and freezes new resource creation until it settles", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    act(() => landing.setLandingWorkspaceBusy(true));
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    expect(authenticatedFetch).not.toHaveBeenCalled();
    act(() => landing.setLandingWorkspaceBusy(false));
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(landing.readLandingWorkspaceState().starting).toBe(true);
    expect(navigateMock).not.toHaveBeenCalled();
    resolveCreate({ ok: true, json: async () => ({ id: "conv_waited" }) } as Response);
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_waited"));
    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(landing.readLandingWorkspaceState().starting).toBe(false);
  });

  it("does not redirect a newer same-URL landing after server-first Start resolves", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("older start");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    landing.advanceLandingNavigationGeneration();
    resolveCreate({ ok: true, json: async () => ({ id: "conv_old" }) } as Response);

    await waitFor(() => expect(setPendingInitialPromptMock).toHaveBeenCalled());
    expect(navigateMock).not.toHaveBeenCalledWith("/c/conv_old");
  });

  it("rejects local handoff navigation after a newer same-URL landing entry", async () => {
    let resolveCreate!: (response: Response) => void;
    window.history.replaceState({ idx: 0 }, "", window.location.href);
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    beginLocalConversationMock.mockReturnValue({
      tempConvId: "temp:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      pendingMsgTempId: "pending-old",
      createToken: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    });
    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("older local start");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    window.history.pushState(
      { ...(window.history.state ?? {}), key: "newer-landing" },
      "",
      window.location.href,
    );
    resolveCreate({ ok: true, json: async () => ({ id: "conv_old" }) } as Response);

    await waitFor(() => expect(hydrateLocalConversationMock).toHaveBeenCalled());
    const mayNavigate = hydrateLocalConversationMock.mock.calls[0]?.[8] as () => boolean;
    expect(mayNavigate()).toBe(false);
  });

  it("posts host_id, workspace and agent_id to /v1/sessions and navigates", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [url, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions");
    expect(init.method).toBe("POST");
    // The host (auto-selected), seeded workspace and default agent must all
    // reach the server. A missing host_id/workspace would create an unbound
    // session; a wrong agent_id would launch the wrong assistant.
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      agent_id: "ag_hello",
      host_id: "host_1",
      workspace: SEEDED_WORKSPACE,
    });
    expect(body.id).toBeUndefined();
    expect(body.labels).toEqual({
      "omnigent.client_create_token": expect.stringMatching(/^[0-9a-f]{32}$/),
    });

    // On success the screen routes to the freshly created session.
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("keeps draft tools on landing until the exact-token pushed row confirms creation", async () => {
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }) as ReturnType<typeof authenticatedFetch>,
    );
    beginLocalConversationMock.mockReturnValue({
      tempConvId: "temp:1234567890abcdef1234567890abcdef",
      pendingMsgTempId: "pend_1",
      createToken: "1234567890abcdef1234567890abcdef",
    });

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
    expect(navigateMock).not.toHaveBeenCalled();
    await waitFor(() => expect(pushMatchers).toHaveLength(1));
    const isOurs = pushMatchers[0]!;
    expect(
      isOurs({
        id: "conv_pushed",
        parent_session_id: null,
        labels: { "omnigent.client_create_token": "1234567890abcdef1234567890abcdef" },
      }),
    ).toBe(true);
    expect(
      isOurs({
        id: "conv_wrong",
        parent_session_id: null,
        labels: { "omnigent.client_create_token": "ffffffffffffffffffffffffffffffff" },
      }),
    ).toBe(false);
    expect(isOurs({ id: "conv_missing", parent_session_id: null })).toBe(false);
    expect(
      isOurs({
        id: "conv_child",
        parent_session_id: "conv_parent",
        labels: { "omnigent.client_create_token": "1234567890abcdef1234567890abcdef" },
      }),
    ).toBe(false);

    act(() =>
      announcePushedSession?.({
        id: "conv_pushed",
        parent_session_id: null,
        labels: { "omnigent.client_create_token": "1234567890abcdef1234567890abcdef" },
      }),
    );
    await waitFor(() =>
      expect(hydrateLocalConversationMock).toHaveBeenCalledWith(
        "temp:1234567890abcdef1234567890abcdef",
        "conv_pushed",
        "ag_hello",
        "inspect the repo",
        [],
        "pend_1",
        null,
        navigateMock,
        expect.any(Function),
        undefined,
      ),
    );
    expect(resolveCreate).toBeTypeOf("function");
  });

  it("keeps a managed create on landing until the HTTP response succeeds", async () => {
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }) as ReturnType<typeof authenticatedFetch>,
    );
    beginLocalConversationMock.mockReturnValue({
      tempConvId: "temp:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      pendingMsgTempId: "pend_managed",
      createToken: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    });

    renderLanding([], { managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    typeMessage("start a sandbox");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
    expect(navigateMock).not.toHaveBeenCalled();
    expect(pushMatchers).toHaveLength(0);
    expect(hydrateLocalConversationMock).not.toHaveBeenCalled();

    resolveCreate({
      ok: true,
      json: async () => ({ id: "conv_managed" }),
    } as unknown as Response);
    await waitFor(() =>
      expect(hydrateLocalConversationMock).toHaveBeenCalledWith(
        "temp:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "conv_managed",
        "ag_hello",
        "start a sandbox",
        [],
        "pend_managed",
        null,
        navigateMock,
        expect.any(Function),
        undefined,
      ),
    );
  });

  it("removes a managed temp conversation when the HTTP response rejects it", async () => {
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }) as ReturnType<typeof authenticatedFetch>,
    );
    beginLocalConversationMock.mockReturnValue({
      tempConvId: "temp:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      pendingMsgTempId: "pend_managed_fail",
      createToken: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    });

    renderLanding([], { managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    typeMessage("start a sandbox");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(pushMatchers).toHaveLength(0);

    resolveCreate({
      ok: false,
      status: 503,
      json: async () => ({ detail: "managed launch rejected" }),
    } as unknown as Response);
    await waitFor(() =>
      expect(removeLocalConversationMock).toHaveBeenCalledWith(
        "temp:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      ),
    );
    expect(hydrateLocalConversationMock).not.toHaveBeenCalled();
  });

  it("preserves restored tools while the initial host and folder defaults hydrate", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    landing.publishLandingWorkspaceSelection({
      hostId: "host_1",
      workspace: SEEDED_WORKSPACE,
      available: true,
      reason: "",
    });
    landing.writeLandingWorkspacePanel({
      openBrowsers: ["restored-browser"],
      selectedBrowserId: "restored-browser",
      selectedTerminalKey: "terminal:restored-shell",
    });
    const namespace = landing.readLandingWorkspaceState().browserNamespace;
    const discard = vi.fn().mockResolvedValue(undefined);
    const unregister = landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(true),
    });
    try {
      renderLanding();
      await waitForWorkspaceSeed();
      expect(landing.readLandingWorkspaceState().browserNamespace).toBe(namespace);
      expect(landing.readLandingWorkspaceState().panel.openBrowsers).toEqual(["restored-browser"]);
      expect(discard).not.toHaveBeenCalled();
    } finally {
      unregister();
    }
  });

  it("keeps the previous workspace when a remounted project change is cancelled", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    navigateMock.mockImplementation((to: string) => {
      const query = to.split("?", 2)[1];
      searchParams = new URLSearchParams(query ?? "");
    });
    searchParams = new URLSearchParams("project=Project A");
    projects = [
      { id: "project-a", name: "Project A" },
      { id: "project-b", name: "Project B" },
    ];
    projectConfigs = {
      "project-a": { host_id: "host_1", workspace: SEEDED_WORKSPACE },
      "project-b": { host_id: "host_1", workspace: "/Users/corey/project-b" },
    };
    renderLanding();
    await waitForWorkspaceSeed();
    await waitFor(() =>
      expect(landing.readLandingWorkspaceState().selection?.workspace).toBe(SEEDED_WORKSPACE),
    );
    const previous = landing.readLandingWorkspaceState();
    const discard = vi.fn().mockResolvedValue(undefined);
    const unregister = landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard,
      adopt: vi.fn().mockResolvedValue(true),
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    try {
      cleanup();
      searchParams = new URLSearchParams("project=Project B");
      const remounted = renderLanding();

      await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
      await waitFor(() =>
        expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
      );
      expect(discard).not.toHaveBeenCalled();
      expect(landing.readLandingWorkspaceState()).toEqual(previous);
      expect(navigateMock).toHaveBeenCalledWith("/?project=Project+A", { replace: true });

      searchParams = new URLSearchParams("project=Project A");
      remounted.rerender(<NewChatLandingScreen />);
      await waitFor(() =>
        expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
      );
      expect(confirm).toHaveBeenCalledOnce();
    } finally {
      unregister();
    }
  });

  it("gives concurrent Starts exactly one owner of the draft shells and browser tabs", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    const responses: ((response: Response) => void)[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          responses.push(resolve);
        }),
    );
    renderLanding();
    await waitForWorkspaceSeed();
    const adopt = vi.fn().mockResolvedValue(true);
    const unregister = landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: vi.fn().mockResolvedValue(undefined),
      adopt,
    });
    act(() =>
      landing.writeLandingWorkspacePanel({
        selectedTerminalKey: "terminal:draft-shell",
        openBrowsers: ["draft-browser"],
        selectedBrowserId: "draft-browser",
      }),
    );
    const firstNamespace = landing.readLandingWorkspaceState().browserNamespace;
    const browserAdoptDraft = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserAdoptDraft },
    });
    try {
      typeMessage("first owner");
      fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
      await waitFor(() => expect(responses).toHaveLength(1));
      cleanup();
      renderLanding();
      await waitForWorkspaceSeed();
      typeMessage("second independent chat");
      fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
      await waitFor(() => expect(responses).toHaveLength(2));
      responses[1]!({ ok: true, json: async () => ({ id: "conv_second" }) } as Response);
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_second"));
      expect(adopt).not.toHaveBeenCalled();
      expect(browserAdoptDraft).not.toHaveBeenCalledWith(firstNamespace, "conv_second");
      expect(landing.readLandingWorkspaceState().starting).toBe(true);
      responses[0]!({ ok: true, json: async () => ({ id: "conv_first" }) } as Response);
      await waitFor(() => expect(adopt).toHaveBeenCalledExactlyOnceWith("conv_first"));
      expect(
        browserAdoptDraft.mock.calls.filter(([namespace]) => namespace === firstNamespace),
      ).toEqual([[firstNamespace, "conv_first"]]);
      await waitFor(() => expect(landing.readLandingWorkspaceState().starting).toBe(false));
    } finally {
      unregister();
      Reflect.deleteProperty(window, "omnigentDesktop");
    }
  });

  it("cleans only the captured tools when an older Start fails after switching workspace", async () => {
    const landing = await import("@/lib/landingWorkspaceState");
    let resolveCreate!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    renderLanding();
    await waitForWorkspaceSeed();
    const discardA = vi.fn().mockResolvedValue(undefined);
    const unregisterA = landing.registerLandingResourceLifecycle({
      hasTerminals: () => true,
      discard: discardA,
      adopt: vi.fn().mockResolvedValue(true),
    });
    const namespaceA = landing.readLandingWorkspaceState().browserNamespace;
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, "omnigentDesktop", {
      configurable: true,
      value: { browserClose },
    });
    let unregisterB: (() => void) | undefined;
    try {
      typeMessage("older start");
      fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
      await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
      act(() =>
        landing.publishLandingWorkspaceSelection({
          hostId: "host_1",
          workspace: "/other-folder",
          available: true,
          reason: "",
        }),
      );
      const namespaceB = landing.readLandingWorkspaceState().browserNamespace;
      const discardB = vi.fn().mockResolvedValue(undefined);
      unregisterB = landing.registerLandingResourceLifecycle({
        hasTerminals: () => true,
        discard: discardB,
        adopt: vi.fn().mockResolvedValue(true),
      });
      expect(discardA).not.toHaveBeenCalled();
      resolveCreate({
        ok: false,
        status: 500,
        json: async () => ({ detail: "create failed" }),
      } as Response);
      await waitFor(() => expect(discardA).toHaveBeenCalledTimes(1));
      expect(browserClose).toHaveBeenCalledWith(namespaceA);
      expect(browserClose).not.toHaveBeenCalledWith(namespaceB);
      expect(discardB).not.toHaveBeenCalled();
      expect(landing.readLandingWorkspaceState().browserNamespace).toBe(namespaceB);
    } finally {
      unregisterB?.();
      unregisterA();
      Reflect.deleteProperty(window, "omnigentDesktop");
    }
  });

  it("keeps a failed create's restored draft when a newer create succeeds", async () => {
    let resolveFirst!: (response: Response) => void;
    let resolveSecond!: (response: Response) => void;
    vi.mocked(authenticatedFetch)
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }) as ReturnType<typeof authenticatedFetch>,
      )
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveSecond = resolve;
        }) as ReturnType<typeof authenticatedFetch>,
      );
    beginLocalConversationMock
      .mockReturnValueOnce({
        tempConvId: "temp:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pendingMsgTempId: "pend_a",
        createToken: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      })
      .mockReturnValueOnce({
        tempConvId: "temp:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pendingMsgTempId: "pend_b",
        createToken: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      });

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("restore this draft");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    cleanup();

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("newer successful create");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(pushMatchers).toHaveLength(2);
    const firstRow = {
      id: "conv_first",
      parent_session_id: null,
      labels: { "omnigent.client_create_token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
    };
    const secondRow = {
      id: "conv_second",
      parent_session_id: null,
      labels: { "omnigent.client_create_token": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" },
    };
    expect(pushMatchers[0]?.(firstRow)).toBe(true);
    expect(pushMatchers[0]?.(secondRow)).toBe(false);
    expect(pushMatchers[1]?.(firstRow)).toBe(false);
    expect(pushMatchers[1]?.(secondRow)).toBe(true);
    cleanup();

    resolveFirst({
      ok: false,
      status: 500,
      json: async () => ({ detail: "first create failed" }),
    } as unknown as Response);
    await waitFor(() =>
      expect(removeLocalConversationMock).toHaveBeenCalledWith(
        "temp:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      ),
    );

    resolveSecond({
      ok: true,
      json: async () => ({ id: "conv_second" }),
    } as unknown as Response);
    await waitFor(() =>
      expect(hydrateLocalConversationMock).toHaveBeenCalledWith(
        "temp:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "conv_second",
        "ag_hello",
        "newer successful create",
        [],
        "pend_b",
        null,
        navigateMock,
        expect.any(Function),
        undefined,
      ),
    );

    renderLanding();
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("restore this draft");
  });

  it("records the launched workspace under its host without corrupting other recents", async () => {
    // Write-back hygiene for omnigent:recent-workspaces: the launched path
    // moves to the front of ITS host's list (deduplicated, not appended
    // twice), and other hosts' lists survive untouched. A corrupted or
    // cross-host write here is what later feeds recent[0] into the composer's
    // generic workspace seeding.
    localStorage.setItem(
      RECENT_KEY,
      JSON.stringify({
        host_1: ["/Users/corey/projects/other", SEEDED_WORKSPACE],
        host_2: ["/srv/elsewhere"],
      }),
    );
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    // The auto-seed takes the most-recent path for host_1.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("other"),
    );
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));

    const stored = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "{}") as Record<string, string[]>;
    // Launched path stays a single front entry — re-launching the same
    // workspace must not insert a duplicate or reorder the rest.
    expect(stored.host_1).toEqual(["/Users/corey/projects/other", SEEDED_WORKSPACE]);
    // Another host's recents are untouched by the write-back.
    expect(stored.host_2).toEqual(["/srv/elsewhere"]);
  });

  it("opens the session on the stream's announcement instead of waiting for the create", async () => {
    // POST /v1/sessions doesn't answer until the host has spawned a runner — a
    // process boot, seconds of it — but the session row exists, and is
    // announced on the updates stream, almost immediately. Hold the POST open
    // for the whole test: if the screen still routes, it routed on the
    // announcement, which is the entire point.
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>(() => {}) as ReturnType<typeof authenticatedFetch>,
    );

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(announcePushedSession).not.toBeNull());
    const [, createInit] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const token = JSON.parse(createInit.body as string).labels["omnigent.client_create_token"];
    act(() =>
      announcePushedSession?.({
        id: "conv_pushed",
        parent_session_id: null,
        labels: { "omnigent.client_create_token": token },
      }),
    );

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_pushed"));
    // The first message is handed off under the same id. That coupling is why
    // the id has to be RIGHT and not merely early — a wrong one would post the
    // user's message into somebody else's conversation.
    expect(setPendingInitialPromptMock).toHaveBeenCalledWith(
      "conv_pushed",
      expect.objectContaining({ text: "inspect the repo" }),
    );
  });

  it("recognizes only the session it just asked for among the stream's pushes", async () => {
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>(() => {}) as ReturnType<typeof authenticatedFetch>,
    );

    renderLanding(["conv_existing"]);
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(pushMatchers).toHaveLength(1));
    const isOurs = pushMatchers[0]!;
    const [, createInit] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const token = JSON.parse(createInit.body as string).labels["omnigent.client_create_token"];
    const ours: SessionListWireItem = {
      id: "conv_mine",
      parent_session_id: null,
      labels: { "omnigent.client_create_token": token },
    };
    expect(isOurs(ours)).toBe(true);

    expect(
      isOurs({
        ...ours,
        labels: { "omnigent.client_create_token": "ffffffffffffffffffffffffffffffff" },
      }),
    ).toBe(false);
    expect(isOurs({ ...ours, labels: undefined })).toBe(false);
    expect(isOurs({ ...ours, parent_session_id: "conv_parent" })).toBe(false);
  });

  it("shows a busy spinner on the submit button while the create is in flight", async () => {
    // The create awaits the backend (session bootstrap + worktree setup) before
    // navigating, so the landing screen lingers for the whole round-trip. Hold
    // the POST open with a deferred promise to freeze that window, and assert
    // the submit button flips to a busy/spinning state so the click reads as
    // "working", not "frozen". Without feedback the button just goes inert and
    // the message sits in the composer, so the user thinks nothing was sent.
    let resolveCreate!: (res: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }) as ReturnType<typeof authenticatedFetch>,
    );

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");

    const submit = screen.getByTestId("new-chat-landing-submit");
    // Before submitting, the button is idle: enabled, not busy, arrow (no spin).
    expect(submit).not.toBeDisabled();
    expect(submit).toHaveAttribute("aria-busy", "false");
    expect(submit.querySelector(".animate-spin")).toBeNull();

    fireEvent.click(submit);

    // While the POST is pending the button is disabled + aria-busy, its label
    // reflects the in-flight state, and the spinner icon is mounted.
    await waitFor(() => expect(submit).toBeDisabled());
    expect(submit).toHaveAttribute("aria-busy", "true");
    expect(submit).toHaveAttribute("aria-label", "Starting session");
    expect(submit.querySelector(".animate-spin")).not.toBeNull();
    // Navigation hasn't happened yet — we're still in the "frozen" window.
    expect(navigateMock).not.toHaveBeenCalled();

    // Let the backend respond: the flow completes and navigates away.
    resolveCreate({ ok: true, json: async () => ({ id: "conv_new" }) } as unknown as Response);
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("keeps the seeded working directory when the already-selected host is re-picked", async () => {
    renderLanding();
    await waitForWorkspaceSeed();

    // The first online host auto-selects, so the menu row the user is most
    // likely to click is the one that's already active. Re-picking it must
    // not clear the seeded directory: selectHost used to setWorkspace("")
    // unconditionally, and on a same-host pick none of the seeding effect's
    // inputs (host id, recents, derived home) change, so nothing ever
    // re-filled the field — the chip dropped back to its empty placeholder.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-host-host_1"));

    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo");
  });

  it("does not create a session when Enter is pressed with an empty message", async () => {
    // Host, agent and workspace all seed automatically, so the only thing
    // gating submit is a non-empty message. The Send button is disabled in
    // this state, but Enter calls handleCreate() directly — its guard must
    // mirror canSubmit (the disabled condition) or this path POSTs a
    // blank-prompt session behind the disabled button. Regression for the
    // empty-message bug.
    renderLanding();
    await waitForWorkspaceSeed();

    // Submit button reflects the gate: disabled while the message is empty.
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();

    // Enter on the empty textarea must be a no-op, not a create.
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-input"), { key: "Enter" });

    // No POST fired and no navigation happened — the guard short-circuited.
    // Before the fix the old guard (host/agent/workspace/creating only) let
    // this through and created an unintended empty session.
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("does not create a session when Enter confirms active IME composition", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "オムニジェント" } });

    fireEvent.keyDown(input, { key: "Enter" });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("does not create a session when Enter carries the IME keyCode 229 fallback", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.change(input, { target: { value: "omnigent" } });

    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("hands the sanitized message to the chatStore, not the create body", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Surrounding whitespace + an embedded control char (\x07 bell) prove the
    // screen sanitizes the message before handing it off.
    typeMessage("  read the README\x07 and refactor  ");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on a required field so the absence checks below can't pass
    // vacuously against a malformed/empty body.
    expect(body.agent_id).toBe("ag_hello");
    // The prompt must NOT ride in the create body: for host sessions
    // initial_items are persisted history-only and never fire a turn, so the
    // agent would never respond. It goes through the normal message path from
    // ChatPage instead.
    expect(body.initialPrompt).toBeUndefined();
    expect(body.initial_items).toBeUndefined();

    // It's stashed in the chatStore (keyed by the new conversation id),
    // trimmed + control-char-stripped, for ChatPage to auto-send. Plain
    // text (no leading "/") carries no skill invocation.
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "read the README and refactor",
        skill: null,
        files: [],
      }),
    );
    expect(navigateMock).toHaveBeenCalledWith("/c/conv_new");
  });

  it("stashes the first prompt as the session's optimistic label", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("  read the README\nand refactor  ");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The sidebar reads this while the server's seed title is still in
    // flight — same whitespace-collapsed text the seed will carry, so the
    // swap is invisible.
    await waitFor(() =>
      expect(getOptimisticTitle("conv_new")).toBe("read the README and refactor"),
    );
    expect(navigateMock).toHaveBeenCalledWith("/c/conv_new");
  });

  it("carries attached files into the chatStore handoff", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const file = new File(["x"], "diagram.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
      target: { files: [file] },
    });
    typeMessage("what is in this image?");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The picked File rides the pending-prompt handoff so ChatPage's
    // auto-dispatched first turn sends it — files never go in the create
    // body (same reason as the prompt text: initial_items never fire a turn).
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "what is in this image?",
        skill: null,
        files: [file],
      }),
    );
  });

  it("hands a bundled-skill first message off as a structured invocation", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    setAgents([
      agent({
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("/review-pr 123 focus on auth");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The skill payload is what ChatPage's auto-send keys off to post a
    // slash_command instead of a plain message. If matching regressed (or
    // the handoff dropped the skill), the agent would receive literal
    // "/review-pr 123 focus on auth" text — the original bug.
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/review-pr 123 focus on auth",
        skill: { name: "review-pr", args: "123 focus on auth" },
        files: [],
      }),
    );
  });

  it("keeps an unknown slash command as plain text (no skill payload)", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    setAgents([
      agent({
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Not a bundled skill — e.g. a typo or a host-discovered skill the
    // server can't know pre-session. Falls through to plain text, same as
    // the in-session composer's unknown-command path.
    typeMessage("/typo do something");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/typo do something",
        skill: null,
        files: [],
      }),
    );
  });

  it("keeps slash text plain for native terminal agents", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    // A native agent with a (hypothetical) bundled skill of the same name:
    // the vendor CLI interprets slash commands itself, so the handoff must
    // not intercept them even when the name would match.
    setAgents([
      agent({
        id: "ag_claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("/review-pr 123");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/review-pr 123",
        skill: null,
        files: [],
      }),
    );
  });

  it("records the sanitized prompt in composer history for ArrowUp recall in the new chat", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Same sanitization vehicle as the chatStore handoff test — the history
    // entry must be the SENT prompt (control-char stripped, trimmed), so a
    // recall + resend reproduces exactly what was sent.
    typeMessage("  read the README\x07 and refactor  ");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
    // appendPromptHistoryEntry is unmocked, so it really wrote to conv_new's
    // scoped key — the one the chat composer reads once bound to that session.
    const history = JSON.parse(localStorage.getItem(PROMPT_HISTORY_KEY) ?? "[]");
    // The stored entry is the SANITIZED prompt: the \x07 bell is gone (proving
    // sanitizeInitialPrompt ran — a bare trim would have kept it) and the
    // surrounding whitespace is trimmed. So a recall + resend reproduces
    // exactly what was sent, not the raw keystrokes.
    expect(history[0]).not.toContain("\x07");
    expect(history).toEqual(["read the README and refactor"]);
  });

  it("attaches terminal-wrapper labels when the claude-native agent is chosen", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("do the thing");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // The claude-native session opens terminal-first; these labels are what
    // the UI keys off to render the terminal wrapper. Dropping them would make
    // a native Claude Code session render as a plain chat.
    expect(body.labels).toEqual({
      "omnigent.ui": "terminal",
      "omnigent.wrapper": "claude-code-native-ui",
      "omnigent.client_create_token": expect.stringMatching(/^[0-9a-f]{32}$/),
    });
  });

  it("attaches terminal-wrapper labels when the antigravity-native agent is chosen", async () => {
    setAgents([
      agent({ id: "ag_agy", name: "antigravity-native-ui", display_name: "Antigravity" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_agy" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("do the thing");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // antigravity-native opens terminal-first too; the wrapper value is the
    // agent name (unlike claude, whose wrapper is "claude-code-native-ui").
    // The runner/server key off exactly this value to boot the agy terminal.
    expect(body.labels).toEqual({
      "omnigent.ui": "terminal",
      "omnigent.wrapper": "antigravity-native-ui",
      "omnigent.client_create_token": expect.stringMatching(/^[0-9a-f]{32}$/),
    });
  });

  it("posts --permission-mode <mode> when a non-default mode is picked for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Claude Code's config modal and pick a non-default permission mode,
    // then Save. The create call proves the choice travels as a
    // `--permission-mode <mode>` pair in terminal_launch_args.
    openAgentConfig("ag_native");
    pickSelectOption("new-chat-landing-config-permission", "Bypass permissions");
    saveConfig();
    // The trigger label stays the bare agent name (the pick lives in the modal).
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Exactly the two-token flag pair Claude expects. A wrong value (or a
    // bare single token) means the runner would launch claude with the wrong
    // permission mode.
    expect(body.terminal_launch_args).toEqual(["--permission-mode", "bypassPermissions"]);
    expect(
      JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")["claude-native"]
        ?.mode,
    ).toBe("bypassPermissions");
  });

  it("seeds the permission mode from the last pick for claude-native on a new session", async () => {
    // A returning user's last pick for this harness is on record; the new
    // session must auto-fill it (the "Mode:" pill reflects it) and post it
    // WITHOUT the user re-opening the pill.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { mode: "plan" } }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Seeded without opening the picker — submitting proves the state was
    // pre-filled from storage and rides along to the create.
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.terminal_launch_args).toEqual(["--permission-mode", "plan"]);
  });

  it.each([
    {
      harness: "codex-native",
      agentName: "codex-native-ui",
      displayName: "Codex",
      mode: "full-access",
      expectedArgs: ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    },
    {
      harness: "cursor-native",
      agentName: "cursor-native-ui",
      displayName: "Cursor",
      mode: "plan",
      expectedArgs: ["--mode", "plan"],
    },
    {
      harness: "antigravity-native",
      agentName: "antigravity-native-ui",
      displayName: "Antigravity",
      mode: "skip",
      expectedArgs: ["--dangerously-skip-permissions"],
    },
  ])("seeds the last launched mode for $harness", async (testCase) => {
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ [testCase.harness]: { mode: testCase.mode } }),
    );
    setAgents([
      agent({
        id: `ag_${testCase.harness}`,
        name: testCase.agentName,
        display_name: testCase.displayName,
      }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: `conv_${testCase.harness}` }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).terminal_launch_args).toEqual(testCase.expectedArgs);
  });

  it("persists the picked permission mode for claude-native so the next session seeds it", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_native");
    pickSelectOption("new-chat-landing-config-permission", "Accept edits");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The successfully-created session leaves its launched mode as the next
    // session's default.
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")["claude-native"]
          ?.mode,
      ).toBe("acceptEdits"),
    );
  });

  it("persists Codex bypass so the next session shows it instead of Default", async () => {
    setAgents([agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_codex");
    pickSelectOption("new-chat-landing-config-approval", "Bypass approvals & sandbox");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")["codex-native"]
          ?.mode,
      ).toBe("bypass"),
    );

    cleanup();
    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_codex");
    expect(screen.getByTestId("new-chat-landing-config-approval").textContent).toContain(
      "Bypass approvals & sandbox",
    );
  });

  it("does not leak one harness's mode onto another harness", async () => {
    // Codex has a pick on record; selecting Claude Code (no pick) must stay on
    // its default — modes are keyed per harness, not shared.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "codex-native": { mode: "full-access" } }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Claude Code's config modal: it shows the permission select (not an
    // approval select), and Codex's stored "full-access" preset doesn't bleed
    // in — the permission select sits at its Default.
    openAgentConfig("ag_native");
    expect(screen.queryByTestId("new-chat-landing-config-approval")).toBeNull();
    // The permission select's trigger displays its current value — "Manual"
    // (Claude's own label for the prompting `default` mode), not Codex's
    // stored "full-access" (which isn't even a valid value here).
    expect(screen.getByTestId("new-chat-landing-config-permission").textContent).toContain(
      "Manual",
    );
  });

  it("posts no launch args for opencode-native, even after a codex full-access pick", async () => {
    // OpenCode declares no mode capability (no permission picker) — `opencode
    // attach` has no permission/sandbox CLI flag, and emitting Codex's
    // `--sandbox`/`--ask-for-approval` presets is exactly what crashed the TUI.
    // So a "Full access" pick on Codex must NOT bleed into OpenCode's launch:
    // switching to OpenCode posts no terminal_launch_args at all.
    setAgents([
      agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" }),
      agent({ id: "ag_opencode", name: "opencode-native-ui", display_name: "OpenCode" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_opencode" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick "Full access" for Codex in its config modal and Save.
    openAgentConfig("ag_codex");
    pickSelectOption("new-chat-landing-config-approval", "Full access");
    saveConfig();

    // Switch to OpenCode by clicking its row. It has no mode knobs, so no gear
    // shows for it — its launch posts no terminal_launch_args.
    selectAgent("ag_opencode");

    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.labels?.["omnigent.wrapper"]).toBe("opencode-native-ui");
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("records the launched harness so the picker can promote it later", async () => {
    // The picker promotes previously-launched harnesses out of "More"; this is
    // the write half of that contract. OpenCode isn't fully supported, so
    // without this record it would stay behind "More" forever.
    setAgents([
      agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" }),
      agent({ id: "ag_opencode", name: "opencode-native-ui", display_name: "OpenCode" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_opencode" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    expect(localStorage.getItem("omnigent:recent-harnesses")).toBeNull();

    selectAgent("ag_opencode");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    // Stored under the canonical harness id, not the agent name or wrapper.
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem("omnigent:recent-harnesses") ?? "[]")).toEqual([
        "opencode-native",
      ]),
    );
  });

  it("does not record a harness when the create fails", async () => {
    // Only a successful launch earns a primary slot — a failed create must not
    // promote the harness the user merely attempted.
    setAgents([agent({ id: "ag_opencode", name: "opencode-native-ui", display_name: "OpenCode" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: "boom" }),
      text: async () => "boom",
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem("omnigent:recent-harnesses")).toBeNull();
  });

  it("omits terminal_launch_args when permission mode is left at default for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Untouched default mode → the pill reads as just the agent name, with
    // no "(Default)" suffix.
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on the wrapper label so the absence check below isn't vacuous
    // against a malformed body.
    expect(body.labels?.["omnigent.wrapper"]).toBe("claude-code-native-ui");
    // "Default" → no flag persisted (undefined is dropped by JSON.stringify),
    // so the runner launches claude with its own default.
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("posts --dangerously-skip-permissions when the bypass is picked for antigravity-native", async () => {
    setAgents([
      agent({ id: "ag_agy", name: "antigravity-native-ui", display_name: "Antigravity" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_agy" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_agy");
    pickSelectOption("new-chat-landing-config-agy-skip", "Skip permissions");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // agy's ONLY pre-emptive control, as a bare single token. Claude's
    // `["--permission-mode", ...]` pair would be rejected by agy, which has no
    // such flag — so assert the exact spelling, not merely "some args".
    expect(body.terminal_launch_args).toEqual(["--dangerously-skip-permissions"]);
    expect(
      JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")[
        "antigravity-native"
      ]?.mode,
    ).toBe("skip");
  });

  it("remembers the launched execution mode for cursor-native", async () => {
    setAgents([agent({ id: "ag_cursor", name: "cursor-native-ui", display_name: "Cursor" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_cursor" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_cursor");
    pickSelectOption("new-chat-landing-config-cursor-mode", "Plan");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const stored = JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")[
      "cursor-native"
    ];
    expect(stored?.mode).toBe("plan");
  });

  it("omits terminal_launch_args when antigravity-native permissions are left at default", async () => {
    setAgents([
      agent({ id: "ag_agy", name: "antigravity-native-ui", display_name: "Antigravity" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_agy" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor so the absence check is not vacuous against a malformed body.
    expect(body.labels?.["omnigent.wrapper"]).toBe("antigravity-native-ui");
    // Untouched → agy keeps its own request-review prompt.
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("shows a danger banner while the antigravity-native bypass is selected", async () => {
    setAgents([
      agent({ id: "ag_agy", name: "antigravity-native-ui", display_name: "Antigravity" }),
    ]);
    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_agy");
    // agy exposes no firing pre-tool hook, so Omnigent cannot re-gate tools
    // once this is armed — the banner is the only guardrail the user gets.
    expect(screen.queryByTestId("new-chat-landing-agy-skip-banner")).toBeNull();
    pickSelectOption("new-chat-landing-config-agy-skip", "Skip permissions");
    expect(screen.getByTestId("new-chat-landing-agy-skip-banner")).toBeTruthy();
  });

  it("omits model + effort on create when the picker is untouched for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // No model/effort default is forced: leaving the picker untouched omits
    // both from the create (undefined is dropped by JSON.stringify), so Claude
    // Code launches on its own configured model rather than a UI-forced one.
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  it("rides a picked model + effort along to create for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Model, effort and permission mode share Claude Code's one config modal;
    // both can be set in one visit and commit together on Save.
    openAgentModels("ag_native");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Opus" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "High" }));
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-agent-models"), { key: "Escape" });
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBe("opus");
    expect(body.reasoning_effort).toBe("high");
  });

  it("rides an omni-setup model along to create for pi-native", async () => {
    setAgents([
      agent({
        id: "ag_pi",
        name: "pi-native-ui",
        display_name: "Pi",
        harness: "pi-native",
      }),
    ]);
    vi.mocked(useHostModelOptions).mockReturnValue({
      data: [
        {
          id: "omnigent-openai/system.ai.gpt-5-6-sol",
          model: "omnigent-openai/system.ai.gpt-5-6-sol",
          displayName: "GPT 5.6 Sol",
        },
        {
          id: "omnigent/databricks-claude-sonnet-4-6",
          model: "omnigent/databricks-claude-sonnet-4-6",
          displayName: "Claude Sonnet 4.6",
        },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof useHostModelOptions>);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_pi" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentModels("ag_pi");
    const fullNameRow = document.querySelector(
      '[data-testid="new-chat-landing-agent-model-omnigent-openai/system.ai.gpt-5-6-sol"]',
    );
    expect(fullNameRow).not.toBeNull();
    expect(fullNameRow).toHaveAttribute("title", "GPT 5.6 Sol");
    fireEvent.change(screen.getByTestId("new-chat-landing-agent-model-search"), {
      target: { value: "gpt sol" },
    });
    expect(screen.getByText("GPT 5.6 Sol")).toBeInTheDocument();
    expect(screen.queryByText("Claude Sonnet 4.6")).toBeNull();
    fireEvent.click(screen.getByText("GPT 5.6 Sol"));
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-agent-models"), { key: "Escape" });
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBe("omnigent-openai/system.ai.gpt-5-6-sol");
    expect(body.reasoning_effort).toBeUndefined();
    expect(body.labels?.["omnigent.wrapper"]).toBe("pi-native-ui");
    expect(
      JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")["pi-native"]?.model,
    ).toBe("omnigent-openai/system.ai.gpt-5-6-sol");
  });

  it("seeds the model + effort from the last pick for claude-native on a new session", async () => {
    // A returning user's last model/effort pick for this harness is on record;
    // the new session must auto-fill it and post it WITHOUT re-opening the
    // picker — the same remember-your-pick behavior the permission mode has.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { model: "opus", effort: "high" } }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBe("opus");
    expect(body.reasoning_effort).toBe("high");
  });

  it("persists a picked model for claude-native, preserving the stored effort", async () => {
    // Effort is already on record. Picking only the model must merge — not
    // clobber — so the next session seeds BOTH from storage.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { effort: "high" } }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentModels("ag_native");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Opus" }));
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-agent-models"), { key: "Escape" });
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    // The launched snapshot contains both the new model and seeded effort.
    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")[
        "claude-native"
      ];
      expect(stored?.model).toBe("opus");
      expect(stored?.effort).toBe("high");
    });
  });

  it("ignores a retired stored model id and omits the override on create", async () => {
    // A stale stored model no longer in the picker's vocab must not ride along —
    // resolve to unselected so the create never posts a dead model id (and the
    // valid stored effort still seeds).
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { model: "ancient-model", effort: "high" } }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBe("high");
  });

  it("omits model_override / reasoning_effort for a non-claude-native agent", async () => {
    // hello_world (harness null) has no permission-mode capability, so the
    // model/effort picker never renders and the create carries no model/effort.
    setAgents([agent()]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_x" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.queryByTestId("new-chat-landing-model-trigger")).toBeNull();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  it("posts sandbox + approval args when a non-default preset is picked for codex-native", async () => {
    setAgents([agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Codex's config modal, pick "Full access", and Save.
    openAgentConfig("ag_codex");
    pickSelectOption("new-chat-landing-config-approval", "Full access");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.terminal_launch_args).toEqual([
      "--sandbox",
      "danger-full-access",
      "--ask-for-approval",
      "never",
    ]);
    expect(
      JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")["codex-native"]
        ?.mode,
    ).toBe("full-access");
  });

  it("omits terminal_launch_args when approval mode is left at default for codex-native", async () => {
    setAgents([agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.labels?.["omnigent.wrapper"]).toBe("codex-native-ui");
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("posts harness_override when a brain harness is picked from the harness menu", async () => {
    // polly's spec declares claude-sdk; the harness dropdown offers the
    // override set.
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Polly's config modal and pick the Pi harness, then Save.
    openAgentConfig("ag_polly");
    pickSelectOption("new-chat-landing-config-harness", "Pi");
    saveConfig();
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // The pick must travel at create time — the harness spawns on the first
    // turn, so there is no later surface to apply it.
    expect(body.harness_override).toBe("pi");
    expect(body.agent_id).toBe("ag_polly");
  });

  it("omits harness_override and shows the spec default when no harness is picked", async () => {
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // With no explicit pick the pill shows just the agent name — the spec
    // default is not suffixed (it lives in the Advanced menu's radios).
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Polly");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain(
      "Claude SDK",
    );
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Default kept → no override sent, so the session tracks the agent
    // spec's declared harness even if the bundle updates later.
    expect(body.harness_override).toBeUndefined();
  });

  it("re-picking the spec default clears a previous harness override", async () => {
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick Pi, Save, then change mind back to the spec default (Claude SDK)
    // and Save again.
    openAgentConfig("ag_polly");
    pickSelectOption("new-chat-landing-config-harness", "Pi");
    saveConfig();
    openAgentConfig("ag_polly");
    pickSelectOption("new-chat-landing-config-harness", "Claude SDK");
    saveConfig();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Re-picking the default must CLEAR the override (not post it
    // explicitly) so the session tracks the spec like an untouched one.
    expect(body.harness_override).toBeUndefined();
  });

  // Skipped while the toggle is hidden behind the false-gate in NewChatDialog; un-skip when re-enabling.
  it("no longer renders a standalone smart-routing composer toggle", async () => {
    // The sparkle toggle was folded into the gear modal's Model dropdown — it
    // must not render as a separate composer control anymore.
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.queryByTestId("cost-toggle-trigger")).toBeNull();
  });

  it("renders the config modal footer without its own background or top border", async () => {
    // The Cancel/Save footer should blend into the modal body — no gray tray
    // band and no divider line above the buttons.
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_native");

    const footer = screen
      .getByTestId("new-chat-landing-config-save")
      .closest("[data-slot=dialog-footer]");
    expect(footer).not.toBeNull();
    expect(footer).toHaveClass("bg-transparent", "border-t-0");
    expect(footer?.className).not.toMatch(/bg-muted/);
  });

  it("omits cost_control_mode_override when Smart Routing is left unpicked", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on a required field so the absence check can't pass vacuously.
    expect(body.agent_id).toBe("ag_native");
    // Unset = defer to the spec default; the field must be absent.
    expect(body.cost_control_mode_override).toBeUndefined();
  });

  it("reveals the base-branch field only after a branch name is entered", () => {
    renderLanding();
    openWorktree();
    // Base ref is meaningless without a worktree, so it stays hidden until the
    // user names a branch — then it appears.
    expect(screen.queryByTestId("new-chat-landing-base-branch-input")).toBeNull();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    expect(screen.getByTestId("new-chat-landing-base-branch-input")).toBeInTheDocument();
  });

  // The base branch auto-fills from the Settings › Git default when the user
  // names a new-worktree branch, and is then left to the user: any edit
  // (including clearing it) stands, even when the dropdown is reopened. Only
  // clearing the branch name — starting the worktree over — re-arms the
  // auto-fill so the next named branch seeds from the current default again.
  describe("base-branch field seeding", () => {
    const baseInput = () =>
      screen.getByTestId("new-chat-landing-base-branch-input") as HTMLInputElement;
    const setBranch = (value: string) =>
      fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
        target: { value },
      });
    // The chip toggles the popover; two clicks close-then-reopen it.
    const reopen = () => {
      fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
      fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    };

    it("auto-fills from the stored default when a branch is named", () => {
      localStorage.setItem("omnigent:default-base-branch", "main");
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      expect(baseInput().value).toBe("main");
    });

    it("leaves the field blank when no default is stored, and lets the user type", () => {
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      expect(baseInput().value).toBe("");

      // The user can type freely; it doesn't touch the setting.
      fireEvent.change(baseInput(), { target: { value: "whatever" } });
      expect(baseInput().value).toBe("whatever");
      expect(localStorage.getItem("omnigent:default-base-branch")).toBeNull();
    });

    it("keeps a base the user CLEARED, even after reopening the dropdown", () => {
      // The reported bug: explicitly emptying the base must stick — reopening
      // the dropdown must not re-fill it from the default.
      localStorage.setItem("omnigent:default-base-branch", "main");
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      expect(baseInput().value).toBe("main");

      fireEvent.change(baseInput(), { target: { value: "" } });
      expect(baseInput().value).toBe("");

      reopen();
      expect(baseInput().value).toBe("");
    });

    it("keeps a base the user typed, even after reopening the dropdown", () => {
      localStorage.setItem("omnigent:default-base-branch", "main");
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      fireEvent.change(baseInput(), { target: { value: "release/2.0" } });

      reopen();
      // The user's choice stands — not re-seeded from the default.
      expect(baseInput().value).toBe("release/2.0");
    });

    it("re-arms auto-fill when the branch name is cleared and re-entered", () => {
      localStorage.setItem("omnigent:default-base-branch", "main");
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      fireEvent.change(baseInput(), { target: { value: "custom" } });
      expect(baseInput().value).toBe("custom");

      // Clear the branch name (start the worktree over) — the base field goes
      // away and the auto-fill re-arms.
      setBranch("");
      // Name a branch again: seeds fresh from the current default.
      setBranch("feature/other");
      expect(baseInput().value).toBe("main");
    });

    it("seeds from the current default after it changes, on a re-entered branch", () => {
      localStorage.setItem("omnigent:default-base-branch", "main");
      renderLanding();
      openWorktree();
      setBranch("feature/login");
      expect(baseInput().value).toBe("main");

      // Change the setting, then start the worktree over.
      act(() => writeDefaultBaseBranch("develop"));
      setBranch("");
      setBranch("feature/other");
      expect(baseInput().value).toBe("develop");
    });
  });

  it("posts the stored default base branch without the user touching the field", async () => {
    localStorage.setItem("omnigent:default-base-branch", "main");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openWorktree();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    typeMessage("start the branch");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    // The auto-filled default reaches the server just like a typed base would.
    const body = JSON.parse(init.body as string);
    expect(body.git).toEqual({ branch_name: "feature/login", base_branch: "main" });
  });

  it("posts git.branch_name and git.base_branch when both are provided", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openWorktree();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    fireEvent.change(screen.getByTestId("new-chat-landing-base-branch-input"), {
      target: { value: "main" },
    });
    typeMessage("start the branch");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    // Both the new branch and its base must reach the server so the host
    // creates the worktree off the requested ref, not HEAD.
    const body = JSON.parse(init.body as string);
    expect(body.git).toEqual({ branch_name: "feature/login", base_branch: "main" });
  });

  it("omits base_branch when blank so the host branches from current HEAD", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openWorktree();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    typeMessage("start the branch");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    // No base_branch key (undefined is dropped by JSON.stringify) → the host
    // falls back to the source repo's current HEAD.
    const body = JSON.parse(init.body as string);
    expect(body.git).toEqual({ branch_name: "feature/login" });
  });

  it("surfaces the server's reason and does not navigate on a failed create", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "host is offline" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The error message is shown inline, and we stay on the landing page (no
    // navigation to a session that wasn't created).
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-error").textContent).toContain("host is offline"),
    );
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("remembers the picked agent and preselects it on the next visit", async () => {
    setAgents([agent(), agent({ id: "ag_two", name: "second_agent", display_name: "Second" })]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick the non-default agent (Radix opens on pointerdown). "second_agent"
    // is a custom agent, so it lives in the "Custom agents" submenu.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_two"));
    // The explicit pick persists immediately — no session has to be created
    // for the preference to stick.
    expect(localStorage.getItem("omnigent:last-agent-id")).toBe("ag_two");

    // A fresh mount (the "next visit") must start on the remembered agent:
    // submitting without touching the picker posts ag_two, not the
    // catalog-default ag_hello.
    cleanup();
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("again");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).agent_id).toBe("ag_two");
  });

  it("hydrates the resolved account draft after delayed identity discovery", async () => {
    const viewerId = "viewer@example.test";
    const restoredWorkspace = "/Users/corey/restored-project";
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": { model: "opus", effort: "high" } }),
    );
    setHosts([host(), host({ host_id: "restored-host", name: "restored-host" })]);
    setAgents([
      agent(),
      agent({
        id: "ag_native",
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
      }),
    ]);
    identityState.userId = viewerId;
    const viewerDraftKey = landingStorageKey("omnigent:landing-composer");
    const savedDraft = {
      project: "",
      message: "restore this account draft",
      files: [],
      pickedAgentId: "ag_native",
      selectedHostId: "restored-host",
      sandboxSelected: false,
      sandboxProvider: null,
      sandboxRepoSelections: [],
      workspace: restoredWorkspace,
      branchName: "",
      autoSeededBranch: "",
      prefilledBranch: "",
      permissionMode: "default",
      approvalMode: "on-request",
      bypassSandbox: false,
      cursorExecMode: "ask",
      agySkipMode: "default",
      pickedHarness: null,
      pickedModel: "opus",
      pickedEffort: "high",
      costControlMode: null,
      agentFromConfig: false,
      workspaceFromConfig: false,
    };
    localStorage.setItem(viewerDraftKey, JSON.stringify(savedDraft));
    identityState.userId = null;
    let resolveIdentity: (viewer: string | null) => void = () => {};
    const identityResolved = new Promise<string | null>((resolve) => {
      resolveIdentity = resolve;
    });
    identityState.resolveIdentity = () => identityResolved;

    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("");

    await act(async () => {
      identityState.userId = viewerId;
      resolveIdentity(viewerId);
    });

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-input")).toHaveValue(savedDraft.message),
    );
    expect(screen.getByTestId("new-chat-landing-host-chip")).toHaveAccessibleName(
      "Host: restored-host, Online",
    );
    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain(
      "restored-project",
    );
    expect(JSON.parse(localStorage.getItem(viewerDraftKey) ?? "null")).toMatchObject(savedDraft);
    cleanup();
    expect(JSON.parse(localStorage.getItem(viewerDraftKey) ?? "null")).toMatchObject(savedDraft);

    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-input")).toHaveValue(savedDraft.message),
    );

    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toMatchObject({
      agent_id: "ag_native",
      host_id: "restored-host",
      workspace: restoredWorkspace,
      model_override: "opus",
      reasoning_effort: "high",
    });
  });

  it("falls back to the default agent when the remembered id is no longer listed", async () => {
    // A persisted pick can outlive its agent (unregistered between visits).
    // The stale id must lose to the catalog default — not yield an unusable
    // composer or post a dangling agent_id.
    localStorage.setItem("omnigent:last-agent-id", "ag_gone");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).agent_id).toBe("ag_hello");
  });
});

describe("sanitizeInitialPrompt", () => {
  it.each([
    ["trims surrounding whitespace", "  hello  ", "hello"],
    // \n and \t must survive — multi-line prompts depend on it.
    ["preserves newlines and tabs", "line1\n\tline2", "line1\n\tline2"],
    // C0/C1 controls (bell \x07, NUL \x00, DEL \x7f) corrupt tmux
    // send-keys for native terminal agents, so they're stripped.
    ["strips embedded control chars", "a\x07b\x00c\x7fd", "abcd"],
    // Whitespace-only must collapse so the caller sends nothing.
    ["collapses whitespace-only to empty", "  \n\t ", ""],
    ["returns empty for empty input", "", ""],
  ])("%s", (_label, input, expected) => {
    expect(sanitizeInitialPrompt(input)).toBe(expected);
  });
});

describe("create-session input on touch-primary devices", () => {
  it("Enter inserts a newline instead of submitting when the pointer is coarse", async () => {
    // Phones have no practical Shift+Enter, so an Enter-submit in the
    // create-session composer was an unrecoverable accidental send. On
    // coarse-pointer devices the composer must let Enter fall through to the
    // textarea's native newline; sending stays an explicit tap.
    const matchMediaSpy = vi.spyOn(window, "matchMedia").mockImplementation((query: string) =>
      query.includes("pointer: coarse")
        ? ({
            matches: true,
            media: query,
            onchange: null,
            addListener: () => {},
            removeListener: () => {},
            addEventListener: () => {},
            removeEventListener: () => {},
            dispatchEvent: () => false,
          } as MediaQueryList)
        : ({
            matches: false,
            media: query,
            onchange: null,
            addListener: () => {},
            removeListener: () => {},
            addEventListener: () => {},
            removeEventListener: () => {},
            dispatchEvent: () => false,
          } as MediaQueryList),
    );

    try {
      renderLanding();
      await waitForWorkspaceSeed();
      const input = screen.getByTestId("new-chat-landing-input");
      fireEvent.change(input, { target: { value: "first line of a long prompt" } });

      fireEvent.keyDown(input, { key: "Enter" });

      // No session was created and no navigation happened: Enter was not
      // intercepted, so the composer still holds the user's draft.
      expect(authenticatedFetch).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();

      fireEvent.focus(screen.getByTestId("new-chat-landing-submit"));
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      matchMediaSpy.mockRestore();
    }
  });
});
