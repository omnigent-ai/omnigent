import type * as WorkspacePickerModule from "./WorkspacePicker";
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
import {
  useConversations as useTestConversations,
  moveConversationToProject,
  useProjectConfig,
  useProjects,
} from "@/hooks/useConversations";

vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => useTestConversations(),
  useOptionalLoadedConversations: () => useTestConversations().data,
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
vi.mock("@/lib/sessionHost", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionHostModule>()),
  setSessionHost: vi.fn(),
}));
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as ToastModule from "@/components/ui/toast";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type * as SessionHostModule from "@/lib/sessionHost";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { composerContextToLabels } from "@/lib/composerContextAdapters";
import { createBundledSession, getSessionSlim, launchRunner } from "@/lib/sessionsApi";
import { setSessionHost } from "@/lib/sessionHost";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import type { Host } from "@/hooks/useHosts";
import { useHosts, useHostModelOptions } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import type { ProjectConfig } from "@/lib/projectsApi";
import {
  readLastHostChoice,
  writeLastHostChoice,
  SANDBOX_HOST_CHOICE,
} from "@/lib/hostPreferences";
import {
  readProjectLaunchDefaults,
  writeProjectLaunchDefaults,
} from "@/lib/projectLaunchPreferences";
import { showToast } from "@/components/ui/toast";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";
import { TooltipProvider } from "@/components/ui/tooltip";
import { setOmnigentHostConfig } from "@/lib/host";

// A project-driven visit (`?project=` resolved to a first-class project id)
// creates the session WITH `project_id`: the server files it atomically and
// default-fills config-seeded fields the composer omits. These tests pin the
// new request shape, the field-omission semantics, the skipped follow-up
// move, the legacy paths (label-only, plain visits), and the saved start.
const navigateMock = vi.fn();

const RECENT_KEY = "omnigent:recent-workspaces";
const RECENT_WORKSPACE = "/Users/corey/universe/src/foo";
const REPO = "/Users/corey/projects/alpha";
const EXISTING_WORKTREE = "/Users/corey/worktrees/alpha-feature";

const SOURCE_SESSION: Awaited<ReturnType<typeof getSessionSlim>> = {
  id: "source",
  title: "Coordinator",
  agentId: "ag_source",
  agentName: "source",
  permissionLevel: 4,
  parentSessionId: null,
  subAgentName: null,
  kind: "default",
  status: "idle",
  createdAt: 1,
  items: [],
  hostId: "host_1",
  workspace: REPO,
};

// Mutable so a test can choose a project-driven or plain visit.
let searchParams = new URLSearchParams("project=Alpha");
const replaceSearchParamsMock = vi.fn();
vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ hash: "", state: null }),
  useReplaceSearchParams: () => replaceSearchParamsMock,
  useNavigate: () => navigateMock,
  useSearchParams: () => [searchParams, vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({
  setPendingInitialPrompt: vi.fn(),
}));

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof ToastModule>()),
  showToast: vi.fn(),
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
  useHostWorktrees: vi.fn(),
  hostWorktreesQueryOptions: (hostId: string, repoPath: string) => ({
    queryKey: ["host-worktrees", hostId, repoPath],
    queryFn: async () => [],
  }),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
// The file browser is heavy UI; a stub button stands in for a user selection —
// deliberately re-picking the config workspace through the modal commit path.
vi.mock("./WorkspacePicker", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspacePickerModule>()),
  isNavigablePath: () => false,
  WorkspacePicker: (props: { onSelect: (path: string) => void }) => (
    <button
      type="button"
      data-testid="test-workspace-select"
      onClick={() => props.onSelect(REPO)}
    />
  ),
}));
// Multipart-path plumbing: a fake bundle, a mocked multipart create + runner
// launch, and a CreateAgentDialog stub whose button commits a pending agent.
vi.mock("@/lib/agentBundle", () => ({
  buildAgentBundle: vi.fn(() => Promise.resolve(new File(["x"], "bundle.tar.gz"))),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  createBundledSession: vi.fn(),
  getSessionSlim: vi.fn(),
  launchRunner: vi.fn(),
}));
vi.mock("./CreateAgentDialog", () => ({
  CreateAgentDialog: (props: { onCreate: (input: unknown) => void }) => (
    <button
      type="button"
      data-testid="test-create-pending"
      onClick={() => props.onCreate({ name: "pending-bot", instructions: "hi" })}
    />
  ),
}));
// The projects list + config drive `project_id` resolution; the move helper is
// mocked so the tests can assert it is (not) called without HTTP plumbing.
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: vi.fn(),
  useProjectConfig: vi.fn(),
  moveConversationToProject: vi.fn(),
  // The landing reads useConversations to decide hasNoSessions (the empty-state
  // import affordance); stub it so it doesn't fire an authenticatedFetch that
  // lands at mock.calls[0] and skews these create-POST call assertions.
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
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
    harness: "claude-sdk",
    skills: [],
    ...overrides,
  };
}

function setProjectConfig(config: ProjectConfig | undefined, isLoading = false): void {
  vi.mocked(useProjectConfig).mockReturnValue({ data: config, isLoading } as ReturnType<
    typeof useProjectConfig
  >);
}

function setProjects(
  data: { id: string | null; name: string }[] | undefined,
  isLoading = false,
): void {
  vi.mocked(useProjects).mockReturnValue({ data, isLoading } as ReturnType<typeof useProjects>);
}

/** Serve a git repo (has an is_main worktree) at REPO; [] elsewhere. */
function setRepoIsGit(): void {
  vi.mocked(useHostWorktrees).mockImplementation((hostId, path) => {
    const known = hostId === "host_1" && path === REPO;
    return {
      data: known
        ? ([
            {
              path: REPO,
              branch: "main",
              is_main: true,
              detached: false,
            },
          ] as HostWorktree[])
        : ([] as HostWorktree[]),
      isError: false,
    } as ReturnType<typeof useHostWorktrees>;
  });
}

function renderLanding(infoOverrides: Partial<ServerInfo> = {}): {
  rerender: (ui: ReactNode) => void;
  unmount: () => void;
  client: QueryClient;
} {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = {
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
    features: { harness_install: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...infoOverrides,
  } as ServerInfo;
  const fetch = vi.mocked(authenticatedFetch).getMockImplementation();
  vi.mocked(authenticatedFetch).mockImplementation((url, options) => {
    if (url === "/v1/info") return Promise.resolve(Response.json(info));
    return fetch ? fetch(url, options) : Promise.reject(new Error(`Unexpected request: ${url}`));
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>
          <TooltipProvider>{children}</TooltipProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  const { rerender, unmount } = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return { rerender, unmount, client };
}

/** Open the picker and commit (select + close) an agent by clicking its row. */
function selectAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
  const row = screen.queryByTestId(`new-chat-landing-agent-${agentId}`);
  if (row) fireEvent.keyDown(row, { key: "Escape" });
}

async function submitAndReadBody(
  response: Record<string, unknown> = { id: "conv_new" },
): Promise<Record<string, unknown>> {
  const fetch = vi.mocked(authenticatedFetch).getMockImplementation()!;
  vi.mocked(authenticatedFetch).mockImplementation((url, options) =>
    url === "/v1/sessions" && options?.method === "POST"
      ? Promise.resolve(Response.json(response))
      : fetch(url, options),
  );
  fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
    target: { value: "hello" },
  });
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  await waitFor(() =>
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions",
      expect.objectContaining({ method: "POST" }),
    ),
  );
  // Let the post-create bookkeeping (move / invalidations / navigation) run
  // before reading the body, so move-call assertions can't race the submit.
  await waitFor(() => expect(navigateMock).toHaveBeenCalled());
  const [, init] = vi
    .mocked(authenticatedFetch)
    .mock.calls.find(([url, options]) => url === "/v1/sessions" && options?.method === "POST")!;
  return JSON.parse(init!.body as string) as Record<string, unknown>;
}

beforeEach(() => {
  setOmnigentHostConfig({});
  vi.clearAllMocks();
  navigateMock.mockReset();
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(moveConversationToProject).mockReset();
  vi.mocked(moveConversationToProject).mockResolvedValue({} as never);
  vi.mocked(showToast).mockReset();
  vi.mocked(createBundledSession).mockReset();
  vi.mocked(createBundledSession).mockResolvedValue({ id: "conv_new" });
  vi.mocked(getSessionSlim).mockReset();
  vi.mocked(getSessionSlim).mockResolvedValue(SOURCE_SESSION);
  vi.mocked(launchRunner).mockReset();
  vi.mocked(launchRunner).mockResolvedValue(undefined as never);
  searchParams = new URLSearchParams("project=Alpha");
  resetLandingDraft();
  localStorage.clear();
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [RECENT_WORKSPACE] }));
  vi.mocked(useHosts).mockClear();
  vi.mocked(useHosts).mockReturnValue({ data: [host()] } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [agent(), agent({ id: "ag_other", name: "other", display_name: "Other" })],
  } as ReturnType<typeof useAvailableAgents>);
  setRepoIsGit();
  setProjects([{ id: "proj_alpha", name: "Alpha" }]);
  setProjectConfig({});
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen project-aware create (first-class project_id)", () => {
  it("sends project_id and omits config-seeded agent_id + workspace when nothing was overridden", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    // Config-seeded and untouched: the server default-fills these from the
    // project config, so the request omits them entirely.
    expect("agent_id" in body).toBe(false);
    expect("workspace" in body).toBe(false);
    // The host is not part of the omission contract — still sent explicitly.
    expect(body.host_id).toBe("host_1");
    // Born filed via first-class project_id — no legacy omni_project label.
    expect((body.labels as Record<string, string> | undefined)?.omni_project).toBeUndefined();
  });

  it("sends an explicit agent_id alongside project_id when the user picks a different agent", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Other"),
    );
    selectAgent("ag_hello");

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    // The explicit pick is authoritative — sent so the server can warn on a
    // mismatch instead of silently default-filling the config agent.
    expect(body.agent_id).toBe("ag_hello");
    // The workspace stayed config-seeded, so it remains omitted.
    expect("workspace" in body).toBe(false);
  });

  it("sends a seed from the project's last start explicitly, not for config default-fill", async () => {
    // The server fills an omitted slot from the stored config, which doesn't
    // know about the last start this browser remembers.
    const config = { host_id: "host_1", workspace: REPO, agent_id: "ag_other" };
    setProjectConfig(config);
    writeProjectLaunchDefaults(
      "proj_alpha",
      { agentId: "ag_hello", hostId: "host_1", workspace: RECENT_WORKSPACE },
      config,
    );
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
    );

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.agent_id).toBe("ag_hello");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
  });

  it("keeps a non-project visit explicit and adds only the create token label", async () => {
    searchParams = new URLSearchParams("");
    localStorage.setItem("omnigent:last-agent-id", "ag_hello");
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
    );

    const body = await submitAndReadBody();
    expect(Object.keys(body).sort()).toEqual(["agent_id", "host_id", "labels", "workspace"]);
    expect(body.agent_id).toBe("ag_hello");
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.labels).toEqual({
      "omnigent.client_create_token": expect.stringMatching(/^[0-9a-f]{32}$/),
      ...composerContextToLabels({
        workingDirectory: { kind: "selected", path: RECENT_WORKSPACE },
        worktree: { kind: "none" },
      }),
    });
  });

  it("skips the post-create move when project_id was sent (atomic server-side filing)", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(vi.mocked(moveConversationToProject)).not.toHaveBeenCalled();
  });

  it("keeps the label + follow-up move for a label-only folder (no first-class id)", async () => {
    // The folder exists only as a legacy label: no project row to address by
    // id, so the create cannot send project_id — it stamps the born-filed
    // label and the follow-up move creates the row on demand.
    setProjects([{ id: null, name: "Alpha" }]);
    setProjectConfig(undefined);
    vi.mocked(moveConversationToProject).mockResolvedValue({ project_id: "proj_new" } as never);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
    );

    const body = await submitAndReadBody();
    expect("project_id" in body).toBe(false);
    expect((body.labels as Record<string, string>).omni_project).toBe("Alpha");
    expect(vi.mocked(moveConversationToProject)).toHaveBeenCalledWith("conv_new", "Alpha");
    // The start is remembered under the project row the move created.
    expect(readProjectLaunchDefaults("proj_new", {})).toEqual({
      agentId: "ag_hello",
      pickedHarness: null,
      hostId: "host_1",
      workspace: RECENT_WORKSPACE,
    });
  });

  it("sends agent_id when the user explicitly re-picks the config agent", async () => {
    // Distinguishable only with source tracking: the picked value EQUALS the
    // config value, but the pick is the user's own — it must ride explicitly
    // so a server-side config change can't silently substitute another agent.
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Other"),
    );
    selectAgent("ag_other");

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.agent_id).toBe("ag_other");
    // The workspace was never touched — still omitted.
    expect("workspace" in body).toBe(false);
  });

  it("sends workspace when the user explicitly browses to it, even the config path", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    // Open the working-directory popover, launch the modal, and select the
    // config path — an explicit choice of the exact value the config seeded.
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-open-folder"));
    fireEvent.click(await screen.findByTestId("test-workspace-select"));

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.workspace).toBe(REPO);
    // The agent was never touched — still omitted.
    expect("agent_id" in body).toBe(false);
  });

  it("sends workspace when the user explicitly selects a recent path", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    fireEvent.click(screen.getByTestId("recent-workspace-select-0"));

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect("agent_id" in body).toBe(false);
  });

  it("sends workspace when the user explicitly selects an existing worktree", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    vi.mocked(useHostWorktrees).mockImplementation(
      (hostId, path) =>
        ({
          data:
            hostId === "host_1" && path === REPO
              ? ([
                  {
                    path: REPO,
                    branch: "main",
                    is_main: true,
                    detached: false,
                  },
                  {
                    path: EXISTING_WORKTREE,
                    branch: "feature/alpha",
                    is_main: false,
                    detached: false,
                  },
                ] as HostWorktree[])
              : ([] as HostWorktree[]),
          isError: false,
        }) as unknown as ReturnType<typeof useHostWorktrees>,
    );
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    fireEvent.click(screen.getByRole("radio", { name: "Use worktree alpha-feature" }));

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.workspace).toBe(EXISTING_WORKTREE);
    expect("agent_id" in body).toBe(false);
  });

  it("pins workspace and git to explicit null on a sandbox create under a project", async () => {
    // Absent fields are default-filled from the project config, and a managed
    // create rejects a path workspace / git block — the explicit nulls keep
    // the server from filling them in.
    const config = { host_id: "host_1", workspace: REPO, agent_id: "ag_other" };
    setProjectConfig(config);
    renderLanding({ managed_sandboxes_enabled: true, request_managed_sandbox_host_id: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));

    const body = await submitAndReadBody();
    expect(body.project_id).toBe("proj_alpha");
    expect(body.host_type).toBe("managed");
    expect("workspace" in body).toBe(true);
    expect(body.workspace).toBeNull();
    expect("git" in body).toBe(true);
    expect(body.git).toBeNull();
    expect(body.host_id).toMatch(/^[0-9a-f]{32}$/);
    // The untouched config agent is still omitted for default-fill.
    expect("agent_id" in body).toBe(false);
    // The sandbox, not the config host, is remembered for the next session.
    expect(readProjectLaunchDefaults("proj_alpha", config)).toEqual({
      agentId: "ag_other",
      pickedHarness: null,
      hostId: SANDBOX_HOST_CHOICE,
    });
  });

  it("carries project_id and the omission rules through the multipart (bundled) path", async () => {
    const config = { host_id: "host_1", workspace: REPO, agent_id: "ag_other" };
    setProjectConfig(config);
    vi.mocked(createBundledSession).mockResolvedValue({ id: "conv_new", agentId: "ag_pending" });
    renderLanding({ session_bundle_workspaces: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    // Commit a pending custom agent (via the stubbed dialog), then submit.
    fireEvent.click(screen.getByTestId("test-create-pending"));
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(vi.mocked(createBundledSession)).toHaveBeenCalled());
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());

    // Atomic filing rides in the metadata part; the config-seeded workspace
    // is omitted from the launch fields but preserved in composer context.
    const [, metadata] = vi.mocked(createBundledSession).mock.calls[0];
    expect(metadata).toEqual({
      project_id: "proj_alpha",
      labels: composerContextToLabels({
        workingDirectory: { kind: "selected", path: REPO },
        worktree: { kind: "none" },
      }),
    });
    // The runner still launches with the explicit client-side workspace.
    expect(vi.mocked(launchRunner)).toHaveBeenCalledWith("host_1", "conv_new", REPO, undefined);
    expect(vi.mocked(moveConversationToProject)).not.toHaveBeenCalled();
    // The session-scoped agent the create registered is the start's agent.
    expect(readProjectLaunchDefaults("proj_alpha", config)).toEqual({
      agentId: "ag_pending",
      pickedHarness: null,
      hostId: "host_1",
      workspace: REPO,
    });
  });

  it("omits host_id and skips setSessionHost on a managed bundled create", async () => {
    vi.mocked(setSessionHost).mockClear();
    vi.mocked(createBundledSession).mockResolvedValue({ id: "conv_new", agentId: "ag_pending" });
    renderLanding({ managed_sandboxes_enabled: true, session_bundle_workspaces: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.click(screen.getByTestId("test-create-pending"));
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(vi.mocked(createBundledSession)).toHaveBeenCalled());
    await waitFor(() => expect(navigateMock).toHaveBeenCalled());

    // Bundle creates allocate their sandbox host server-side until multipart host_id ships.
    const metadata = vi.mocked(createBundledSession).mock.calls[0][1] ?? {};
    expect(metadata).toMatchObject({ host_type: "managed" });
    expect("host_id" in metadata).toBe(false);
    expect(setSessionHost).not.toHaveBeenCalled();
  });
});

describe("NewChatLandingScreen nested agent creation", () => {
  it.each(["", "Nested task"])(
    "preserves drafts across composer scopes: %s",
    async (nestedText) => {
      vi.mocked(getSessionSlim).mockImplementation(async (id) => ({ ...SOURCE_SESSION, id }));
      const drafts = [
        { source: null, text: "Standalone task", file: "standalone.txt" },
        { source: "first", text: nestedText, file: nestedText ? "first.txt" : null },
        { source: "second", text: "Another nested task", file: "second.txt" },
      ];
      for (const draft of drafts) {
        searchParams = new URLSearchParams(draft.source ? `sibling=${draft.source}` : "");
        renderLanding({ sibling_sessions: true });
        // oxlint-disable-next-line no-await-in-loop -- Each visit must unmount before the next.
        const input = await screen.findByTestId("new-chat-landing-input");
        expect(input).toHaveValue("");
        fireEvent.change(input, { target: { value: draft.text } });
        if (draft.file) {
          fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
            target: { files: [new File(["notes"], draft.file, { type: "text/plain" })] },
          });
        }
        cleanup();
      }
      for (const draft of drafts) {
        searchParams = new URLSearchParams(draft.source ? `sibling=${draft.source}` : "");
        renderLanding({ sibling_sessions: true });
        // oxlint-disable-next-line no-await-in-loop -- Each visit must unmount before the next.
        expect(await screen.findByTestId("new-chat-landing-input")).toHaveValue(draft.text);
        for (const other of drafts) {
          if (!other.file) continue;
          expect(screen.queryByText(other.file) !== null).toBe(other === draft);
        }
        cleanup();
      }
    },
  );

  it("keeps the ordinary host preference when explicitly selecting the source sandbox", async () => {
    const regular = host({ host_id: "regular", name: "Regular host" });
    const sandbox = host({ sandbox_provider: "modal" });
    vi.mocked(useHosts).mockImplementation(
      (options) =>
        ({
          data: options?.sourceHostId ? [regular, sandbox] : [regular],
        }) as ReturnType<typeof useHosts>,
    );
    writeLastHostChoice("regular");
    localStorage.setItem(RECENT_KEY, JSON.stringify({ regular: [RECENT_WORKSPACE] }));
    searchParams = new URLSearchParams("sibling=source");
    renderLanding({ sibling_sessions: true });
    await screen.findByRole("heading", { name: "Add nested agent" });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(await screen.findByTestId("new-chat-landing-host-host_1"));
    expect(readLastHostChoice()).toBe("regular");
    cleanup();
    resetLandingDraft();
    searchParams = new URLSearchParams();
    renderLanding({ sibling_sessions: true });
    expect(await submitAndReadBody()).toMatchObject({ host_id: "regular" });
  });

  it("waits for the source, resets on source changes, and refuses an unavailable host", async () => {
    const source = SOURCE_SESSION;
    let resolveSource!: (session: typeof source) => void;
    vi.mocked(getSessionSlim).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSource = resolve;
        }),
    );
    searchParams = new URLSearchParams("sibling=source");
    const { rerender } = renderLanding({ sibling_sessions: true });
    expect(screen.getByRole("status")).toHaveTextContent("Loading source session");
    expect(screen.queryByTestId("new-chat-landing-submit")).toBeNull();
    await waitFor(() => expect(resolveSource).toBeDefined());
    resolveSource(source);
    await screen.findByRole("heading", { name: "Add nested agent" });
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "For the first session only" },
    });
    vi.mocked(getSessionSlim).mockResolvedValue({ ...source, id: "other", hostId: "offline" });
    searchParams = new URLSearchParams("sibling=other");
    rerender(<NewChatLandingScreen />);
    await screen.findByRole("heading", { name: "Add nested agent" });
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("");
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "Cannot start on a missing host" },
    });
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-submit").parentElement!);
    expect(screen.getByTestId("new-chat-landing-submit-error-tooltip")).toHaveTextContent(
      "Selected host is unavailable",
    );
    expect(createBundledSession).not.toHaveBeenCalled();
    expect(useHosts).toHaveBeenCalledWith({ refetchOnFocus: true, sourceHostId: "offline" });
  });

  it.each([false, true])(
    "submits the clicked source for JSON and uploaded agents (bundle=%s)",
    async (bundled) => {
      vi.mocked(getSessionSlim).mockImplementation(async (id) => ({ ...SOURCE_SESSION, id }));
      searchParams = new URLSearchParams("sibling=other");
      renderLanding({ sibling_sessions: true });
      fireEvent.change(await screen.findByTestId("new-chat-landing-input"), {
        target: { value: "Keep this other draft" },
      });
      fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
        target: { files: [new File(["other"], "other.txt", { type: "text/plain" })] },
      });
      cleanup();
      searchParams = new URLSearchParams("sibling=source");
      renderLanding({ sibling_sessions: true, session_bundle_workspaces: true });
      await screen.findByRole("heading", { name: "Add nested agent" });
      expect(useHosts).toHaveBeenCalledWith({ refetchOnFocus: true, sourceHostId: "host_1" });
      expect(screen.getByTestId("new-chat-landing-host-chip")).toHaveAttribute(
        "aria-label",
        "Host: This machine, Online",
      );
      fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
        target: { files: [new File(["submitted"], "submitted.txt", { type: "text/plain" })] },
      });
      if (bundled) {
        fireEvent.click(screen.getByTestId("test-create-pending"));
        fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
          target: { value: "Review the API" },
        });
        fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
        await waitFor(() => expect(createBundledSession).toHaveBeenCalled());
        expect(vi.mocked(createBundledSession).mock.calls[0][1]).toMatchObject({
          sibling_source_id: "source",
          workspace: REPO,
        });

        expect(launchRunner).not.toHaveBeenCalled();
      } else {
        expect(await submitAndReadBody()).toMatchObject({
          sibling_source_id: "source",
          host_id: "host_1",
          workspace: REPO,
        });
      }
      await waitFor(() => expect(navigateMock).toHaveBeenCalled());
      cleanup();
      renderLanding({ sibling_sessions: true });
      expect(await screen.findByTestId("new-chat-landing-input")).toHaveValue("");
      expect(screen.queryByText("submitted.txt")).not.toBeInTheDocument();
      cleanup();
      searchParams = new URLSearchParams("sibling=other");
      renderLanding({ sibling_sessions: true });
      expect(await screen.findByTestId("new-chat-landing-input")).toHaveValue(
        "Keep this other draft",
      );
      expect(screen.getByText("other.txt")).toBeVisible();
      expect(screen.queryByText("submitted.txt")).not.toBeInTheDocument();
    },
  );
});

describe("NewChatLandingScreen Add Agent destinations", () => {
  it.each([
    ["current_host", false, false],
    ["current_host_or_new_sandbox", false, true],
    ["any_host", true, true],
  ] as const)(
    "%s controls connected-host and new-sandbox choices",
    async (policy, showsOtherHost, showsSandbox) => {
      setOmnigentHostConfig({ docsLinks: { newSandbox: "Set up managed sandboxes" } });
      searchParams = new URLSearchParams("sibling=source");
      vi.mocked(useHosts).mockReturnValue({
        data: [host(), host({ host_id: "host_2", name: "build-server" })],
      } as ReturnType<typeof useHosts>);
      renderLanding({
        sibling_sessions: true,
        sibling_session_destinations: policy,
        managed_sandboxes_enabled: true,
        sandbox_provider: "modal",
      });

      await screen.findByRole("heading", { name: "Add nested agent" });
      fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
      expect(screen.getByTestId("new-chat-landing-host-host_1")).toBeInTheDocument();
      expect(screen.queryByTestId("new-chat-landing-host-host_2") !== null).toBe(showsOtherHost);
      expect(screen.queryByTestId("new-chat-landing-sandbox-option") !== null).toBe(showsSandbox);
      expect(screen.queryByTestId("new-chat-landing-sandbox-option-disabled")).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-connect-host") !== null).toBe(showsOtherHost);
    },
  );

  it.each([false, true])(
    "clamps a sandbox after a policy change (restored draft=%s)",
    async (restore) => {
      searchParams = new URLSearchParams("sibling=source");
      const first = renderLanding({
        sibling_sessions: true,
        sibling_session_destinations: "any_host",
        managed_sandboxes_enabled: true,
        sandbox_provider: "modal",
      });
      await screen.findByRole("heading", { name: "Add nested agent" });
      fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
      fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
      if (restore) {
        first.unmount();
        renderLanding({
          sibling_sessions: true,
          sibling_session_destinations: "current_host",
          managed_sandboxes_enabled: true,
          sandbox_provider: "modal",
        });
        await screen.findByRole("heading", { name: "Add nested agent" });
      } else {
        first.client.setQueryData(["sharing", "sibling-destinations"], "current_host");
      }
      await waitFor(() =>
        expect(screen.getByTestId("new-chat-landing-workspace-chip")).toHaveTextContent("alpha"),
      );
      fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
      expect(screen.queryByTestId("new-chat-landing-sandbox-option")).toBeNull();
      expect(screen.getByTestId("new-chat-landing-host-host_1")).toBeInTheDocument();
    },
  );
});

describe("NewChatLandingScreen uploaded agent destinations", () => {
  it.each(["folder", "new", "existing", "unsupported"] as const)(
    "binds an uploaded nested agent with %s worktree metadata",
    async (worktree) => {
      searchParams = new URLSearchParams("sibling=source");
      if (worktree !== "folder") {
        vi.mocked(useHostWorktrees).mockReturnValue({
          data: [
            { path: REPO, branch: "main", is_main: true, detached: false },
            { path: EXISTING_WORKTREE, branch: "feature/alpha", is_main: false, detached: false },
          ],
          isError: false,
        } as ReturnType<typeof useHostWorktrees>);
      }
      renderLanding({
        sibling_sessions: true,
        session_bundle_workspaces: worktree !== "unsupported",
      });
      await screen.findByRole("heading", { name: "Add nested agent" });
      fireEvent.click(screen.getByTestId("test-create-pending"));
      if (worktree !== "folder") {
        fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
        if (worktree === "existing") {
          fireEvent.click(screen.getByRole("radio", { name: "Use worktree alpha-feature" }));
        } else {
          fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
            target: { value: "feature/review" },
          });
          fireEvent.keyDown(screen.getByTestId("new-chat-landing-branch-input"), { key: "Escape" });
        }
      }
      fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
        target: { value: "Review the API" },
      });
      if (worktree === "unsupported") {
        expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();
        fireEvent.pointerDown(screen.getByTestId("new-chat-landing-submit").parentElement!);
        expect(screen.getByTestId("new-chat-landing-submit-error-tooltip")).toHaveTextContent(
          "server needs an update to create a custom agent in a worktree",
        );
        expect(createBundledSession).not.toHaveBeenCalled();
        return;
      }
      fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
      await waitFor(() => expect(createBundledSession).toHaveBeenCalled());
      const [, metadata] = vi.mocked(createBundledSession).mock.calls[0];
      expect(metadata).toEqual(
        expect.objectContaining({
          sibling_source_id: "source",
          host_id: "host_1",
          workspace: worktree === "existing" ? EXISTING_WORKTREE : REPO,
        }),
      );
      if (worktree === "folder") expect(metadata?.git).toBeUndefined();
      else
        expect(metadata?.git).toEqual(
          expect.objectContaining(
            worktree === "existing"
              ? { branch_name: "feature/alpha", existing_worktree: true }
              : { branch_name: "feature/review" },
          ),
        );
      expect(launchRunner).not.toHaveBeenCalled();
    },
  );

  it.each([false, true])(
    "delivers an uploaded sibling agent task on a new sandbox (durable=%s)",
    async (durable) => {
      searchParams = new URLSearchParams("sibling=source");
      renderLanding({
        session_bundle_workspaces: true,
        durable_initial_input: durable,
        sibling_sessions: true,
        managed_sandboxes_enabled: true,
        sandbox_provider: "modal",
      });
      await screen.findByRole("heading", { name: "Add nested agent" });

      fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
      fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
      await waitFor(() => expect(vi.mocked(useHostModelOptions).mock.lastCall?.[0]).toBeNull());
      fireEvent.click(screen.getByTestId("test-create-pending"));
      fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
        target: { value: "Review the API" },
      });
      fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
      await waitFor(() => expect(createBundledSession).toHaveBeenCalled());

      const [, metadata] = vi.mocked(createBundledSession).mock.calls[0];
      expect(metadata).toEqual(
        expect.objectContaining({
          sibling_source_id: "source",
          host_type: "managed",
          workspaces: [],
        }),
      );
      await waitFor(() => expect(navigateMock).toHaveBeenCalled());
      const { setPendingInitialPrompt } = await import("@/store/chatStore");
      if (durable) {
        expect(metadata?.initial_input).toEqual({
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: "Review the API" }] },
        });
        expect(setPendingInitialPrompt).not.toHaveBeenCalled();
      } else {
        expect(setPendingInitialPrompt).toHaveBeenCalledWith(
          "conv_new",
          expect.objectContaining({ text: "Review the API" }),
        );
      }
      expect(metadata).not.toHaveProperty("host_id");
      expect(launchRunner).not.toHaveBeenCalled();
    },
  );
});
