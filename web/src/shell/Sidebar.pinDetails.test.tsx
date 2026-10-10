import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as HostsModule from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { resetReadStateForTests } from "@/hooks/useUnseenConversations";
import { ExtensionCatalogProvider } from "@/extensions/ExtensionProvider";
import * as identity from "@/lib/identity";
import { getSession } from "@/lib/sessionsApi";
import { clearOptimisticTitles } from "@/lib/optimisticTitles";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
import { buildComposerSessionDescriptor, composerModelChipLabel } from "@/lib/composerModelLabel";
import { useChatStore } from "@/store/chatStore";
import { Sidebar } from "./Sidebar";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

const listRows = vi.hoisted(() => ({ current: [] as ConversationsModule.Conversation[] }));
const availableAgents = vi.hoisted(() => ({
  current: [] as AvailableAgent[],
}));

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  const { conversationHooksMock, conversationPage } = await import("@/test/sidebarMockHelpers");
  return {
    ...actual,
    ...conversationHooksMock(),
    useConversations: () => conversationPage(listRows.current),
    usePinnedConversations: actual.usePinnedConversations,
    useTogglePinnedConversation: actual.useTogglePinnedConversation,
  };
});
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({
    data: [{ host_id: "host_detail", name: "Remote workstation", status: "online" }],
  }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: availableAgents.current }),
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

beforeEach(() => {
  listRows.current = [];
  availableAgents.current = [];
  localStorage.clear();
  resetReadStateForTests();
  clearSessionDrafts();
  clearOptimisticTitles();
  useChatStore.setState({ conversationId: null, status: "idle", terminalPending: false });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("header-pinned detail-only session tooltip", () => {
  it.each(["codex", null] as const)(
    "preserves the resolved child %s before list reconciliation",
    async (harness) => {
      availableAgents.current = [
        {
          id: "ag_bundle",
          name: "bundle",
          display_name: "Bundle",
          harness: "claude-sdk",
          description: null,
          skills: [],
        },
      ];
      const wire = {
        id: "child_detail",
        agent_id: "ag_bundle",
        agent_name: "bundle",
        title: "Child detail",
        created_at: 100,
        status: "idle",
        parent_session_id: "parent",
        sub_agent_name: "worker",
        harness,
        cost_control_mode_override: "on",
        labels: {},
      };
      let resolvePatch!: (response: Response) => void;
      const patch = new Promise<Response>((resolve) => {
        resolvePatch = resolve;
      });
      const fetchSpy = vi
        .spyOn(identity, "authenticatedFetch")
        .mockImplementation((_url, init) =>
          init?.method === "PATCH" ? patch : Promise.resolve(new Response(JSON.stringify(wire))),
        );
      const session = await getSession(wire.id);
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: Infinity },
          mutations: { retry: false },
        },
      });
      client.setQueryData(["session", session.id], session);
      client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [], filterHonored: true });
      render(
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <ExtensionCatalogProvider extensions={[]}>
              <TooltipProvider>
                <MemoryRouter>
                  <Sidebar open onClose={vi.fn()} />
                  <HeaderConversationMenu
                    conversation={{
                      id: session.id,
                      object: "conversation",
                      title: session.title,
                      labels: {},
                      created_at: 100,
                      updated_at: 100,
                      permission_level: session.permissionLevel,
                    }}
                    currentProject={null}
                    canShare={false}
                    canFork={false}
                    onShare={vi.fn()}
                    onFork={vi.fn()}
                  />
                </MemoryRouter>
              </TooltipProvider>
            </ExtensionCatalogProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      );
      fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
        button: 0,
        ctrlKey: false,
      });
      fireEvent.click(screen.getByTestId("header-pin-conversation"));
      fireEvent.focus(await screen.findByRole("link", { name: "Child detail" }));
      const tooltip = await screen.findByTestId("session-tooltip-content");
      const line = within(tooltip).getAllByTestId("session-tooltip-agent")[0];
      expect(line).toHaveTextContent(harness ? /^Bundle · Codex$/ : /^bundle$/);
      if (harness === null) expect(line.querySelector(".lucide-bot")).not.toBeNull();
      const pinned = client.getQueryData<{ conversations: ConversationsModule.Conversation[] }>(
        PINNED_CONVERSATIONS_KEY,
      )!;
      expect(pinned.conversations[0]).toMatchObject({
        child_harness: harness,
        cost_control_mode_override: "on",
      });
      expect(
        fetchSpy.mock.calls.some(
          ([url]) =>
            String(url).includes("/model-options") || String(url).startsWith("/v1/sessions?"),
        ),
      ).toBe(false);
      await act(async () =>
        resolvePatch(
          new Response(JSON.stringify({ ...wire, labels: { "omnigent.pinned": "123" } })),
        ),
      );
    },
  );

  it.each(["running", "failed"] as const)(
    "shows cached agent, location and %s status before list reconciliation",
    async (status) => {
      const wire = {
        id: "conv_detail",
        agent_id: "ag_native",
        agent_name: "claude-native-ui",
        title: "Detail-only session",
        created_at: 100,
        status,
        harness: "claude-native",
        llm_model: "opus[1m]",
        reasoning_effort: "medium",
        host_id: "host_detail",
        runner_id: "runner_detail",
        host_online: true,
        runner_online: true,
        workspace: "/srv/remote/repo",
        git_branch: "feat/detail-pin",
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      };
      let resolvePatch!: (response: Response) => void;
      const patch = new Promise<Response>((resolve) => {
        resolvePatch = resolve;
      });
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockImplementation((url, init) => {
        if (init?.method === "PATCH") return patch;
        if (String(url).includes("/model-options")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
              }),
            ),
          );
        }
        return Promise.resolve(new Response(JSON.stringify(wire)));
      });
      const session = await getSession(wire.id);
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: Infinity },
          mutations: { retry: false },
        },
      });
      client.setQueryData(["session", session.id], session);
      client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [], filterHonored: true });
      render(
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <ExtensionCatalogProvider extensions={[]}>
              <TooltipProvider>
                <MemoryRouter initialEntries={[`/c/${session.id}`]}>
                  <Sidebar open onClose={vi.fn()} />
                  <HeaderConversationMenu
                    conversation={{
                      id: session.id,
                      object: "conversation",
                      title: session.title,
                      labels: {},
                      created_at: session.createdAt,
                      updated_at: session.createdAt,
                      permission_level: session.permissionLevel,
                    }}
                    currentProject={null}
                    canShare={false}
                    canFork={false}
                    onShare={vi.fn()}
                    onFork={vi.fn()}
                  />
                </MemoryRouter>
              </TooltipProvider>
            </ExtensionCatalogProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      );
      expect(screen.queryByRole("link", { name: session.title! })).toBeNull();
      fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
        button: 0,
        ctrlKey: false,
      });
      fireEvent.click(screen.getByTestId("header-pin-conversation"));
      const row = await screen.findByRole("link", { name: session.title! });
      fireEvent.focus(row);
      const tooltip = await screen.findByTestId("session-tooltip-content");
      expect(within(tooltip).getAllByTestId("session-tooltip-location")[0]).toHaveTextContent(
        "Remote workstation",
      );
      const state = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      expect(state).toHaveTextContent(status === "running" ? "Working" : "Error");
      expect(state).toHaveAttribute("data-state", status === "running" ? "working" : "error");
      await waitFor(() =>
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          /^Opus 5.5 1M Medium$/,
        ),
      );
      expect(within(tooltip).getAllByTestId("session-tooltip-cwd")[0]).toHaveTextContent(
        wire.workspace,
      );
      expect(within(tooltip).getAllByTestId("session-tooltip-branch")[0]).toHaveTextContent(
        wire.git_branch,
      );
      expect(client.getQueriesData({ queryKey: ["conversations"] })).toHaveLength(0);
      expect(client.getQueriesData({ queryKey: ["project-sessions"] })).toHaveLength(0);
      expect(fetchSpy.mock.calls.some(([url]) => String(url).startsWith("/v1/sessions?"))).toBe(
        false,
      );
      await act(async () =>
        resolvePatch(
          new Response(
            JSON.stringify({ ...wire, labels: { ...wire.labels, "omnigent.pinned": "123" } }),
          ),
        ),
      );
      client.clear();
    },
  );

  it("shows a cached model override instead of the spec model before reporting or reconciliation", async () => {
    const wire = {
      id: "conv_override",
      agent_id: "ag_native",
      agent_name: "claude-native-ui",
      title: "Override-only session",
      created_at: 100,
      status: "running",
      harness: "claude-native",
      llm_model: "spec-model",
      model_override: "opus[1m]",
      reasoning_effort: "medium",
      host_id: "host_detail",
      host_online: true,
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
    };
    let resolvePatch!: (response: Response) => void;
    const patch = new Promise<Response>((resolve) => {
      resolvePatch = resolve;
    });
    const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockImplementation((url, init) => {
      if (init?.method === "PATCH") return patch;
      if (String(url).includes("/model-options")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
            }),
          ),
        );
      }
      return Promise.resolve(new Response(JSON.stringify(wire)));
    });
    const session = await getSession(wire.id);
    expect(session.llmModel).toBe(wire.llm_model);
    expect(session.modelOverride).toBe(wire.model_override);
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity },
        mutations: { retry: false },
      },
    });
    client.setQueryData(["session", session.id], session);
    client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [], filterHonored: true });
    render(
      <QueryClientProvider client={client}>
        <SidebarDataProvider>
          <ExtensionCatalogProvider extensions={[]}>
            <TooltipProvider>
              <MemoryRouter initialEntries={[`/c/${session.id}`]}>
                <Sidebar open onClose={vi.fn()} />
                <HeaderConversationMenu
                  conversation={{
                    id: session.id,
                    object: "conversation",
                    title: session.title,
                    labels: {},
                    created_at: session.createdAt,
                    updated_at: session.createdAt,
                    permission_level: session.permissionLevel,
                  }}
                  currentProject={null}
                  canShare={false}
                  canFork={false}
                  onShare={vi.fn()}
                  onFork={vi.fn()}
                />
              </MemoryRouter>
            </TooltipProvider>
          </ExtensionCatalogProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(screen.getByTestId("header-pin-conversation"));
    fireEvent.focus(await screen.findByRole("link", { name: session.title! }));
    const tooltip = await screen.findByTestId("session-tooltip-content");
    await waitFor(() =>
      expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
        /^Opus 5.5 1M Medium$/,
      ),
    );
    expect(
      fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
    ).toHaveLength(1);
    expect(fetchSpy.mock.calls.some(([url]) => String(url).startsWith("/v1/sessions?"))).toBe(
      false,
    );
    expect(client.getQueriesData({ queryKey: ["conversations"] })).toHaveLength(0);
    await act(async () =>
      resolvePatch(
        new Response(
          JSON.stringify({ ...wire, labels: { ...wire.labels, "omnigent.pinned": "123" } }),
        ),
      ),
    );
    client.clear();
  });

  it("matches the composer and normal list row effort for a label-less detail-only Devin pin", async () => {
    const wire = {
      id: "conv_devin_detail",
      agent_id: "ag_devin",
      agent_name: "devin-native-ui",
      title: "Detail-only Devin",
      created_at: 100,
      status: "running",
      harness: "devin-native",
      llm_model: "devin-model",
      reasoning_effort: "high",
      host_id: "host_detail",
      host_online: true,
      labels: {},
    };
    const modelOptions = [
      {
        id: "devin-model",
        displayName: "Devin model",
        supportedReasoningEfforts: [{ reasoningEffort: "high" }],
      },
    ];
    let resolvePatch!: (response: Response) => void;
    const patch = new Promise<Response>((resolve) => {
      resolvePatch = resolve;
    });
    const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockImplementation((url, init) => {
      if (init?.method === "PATCH") return patch;
      if (String(url).includes("/model-options")) {
        return Promise.resolve(new Response(JSON.stringify({ models: modelOptions })));
      }
      return Promise.resolve(new Response(JSON.stringify(wire)));
    });
    const session = await getSession(wire.id);
    const normalRow: ConversationsModule.Conversation = {
      id: "conv_devin_list",
      object: "conversation",
      title: "Listed Devin",
      created_at: 100,
      updated_at: 100,
      labels: {},
      permission_level: null,
      agent_id: wire.agent_id,
      agent_name: wire.agent_name,
      host_id: wire.host_id,
      host_online: true,
      harness_override: null,
      llm_model: wire.llm_model,
      reasoning_effort: wire.reasoning_effort,
      status: "running",
    };
    listRows.current = [normalRow];
    const chipInputs = {
      modelSummary: "Devin model",
      nativeDisplayName: "Devin",
      model: session.llmModel,
      modelOptions,
      effort: session.reasoningEffort,
    };
    const composerChip = composerModelChipLabel({
      ...chipInputs,
      session: buildComposerSessionDescriptor(
        session.harness,
        session.labels,
        session.parentSessionId,
        session.inferenceConfigured,
      ),
    });
    const listChip = composerModelChipLabel({
      ...chipInputs,
      session: buildComposerSessionDescriptor(
        "devin-native",
        normalRow.labels,
        normalRow.parent_session_id,
      ),
    });
    expect(composerChip).toEqual({ label: "Devin model", effortLabel: null });
    expect(listChip).toEqual(composerChip);
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: Infinity },
        mutations: { retry: false },
      },
    });
    client.setQueryData(["session", session.id], session);
    client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [], filterHonored: true });
    render(
      <QueryClientProvider client={client}>
        <SidebarDataProvider>
          <ExtensionCatalogProvider extensions={[]}>
            <TooltipProvider>
              <MemoryRouter initialEntries={[`/c/${session.id}`]}>
                <Sidebar open onClose={vi.fn()} />
                <HeaderConversationMenu
                  conversation={{
                    id: session.id,
                    object: "conversation",
                    title: session.title,
                    labels: {},
                    created_at: session.createdAt,
                    updated_at: session.createdAt,
                    permission_level: session.permissionLevel,
                  }}
                  currentProject={null}
                  canShare={false}
                  canFork={false}
                  onShare={vi.fn()}
                  onFork={vi.fn()}
                />
              </MemoryRouter>
            </TooltipProvider>
          </ExtensionCatalogProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );
    fireEvent.focus(screen.getByRole("link", { name: normalRow.title! }));
    const listedTooltip = await screen.findByTestId("session-tooltip-content");
    await waitFor(() =>
      expect(
        within(listedTooltip).getAllByTestId("session-tooltip-agent")[0].querySelector("span"),
      ).toHaveTextContent(/^Devin model$/),
    );
    fireEvent.blur(screen.getByRole("link", { name: normalRow.title! }));
    await waitFor(() => expect(screen.queryByTestId("session-tooltip-content")).toBeNull());
    fireEvent.pointerDown(screen.getByTestId("header-conversation-actions"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(screen.getByTestId("header-pin-conversation"));
    fireEvent.focus(await screen.findByRole("link", { name: session.title! }));
    const pinnedTooltip = await screen.findByTestId("session-tooltip-content");
    expect(
      within(pinnedTooltip).getAllByTestId("session-tooltip-agent")[0].querySelector("span"),
    ).toHaveTextContent(/^Devin model$/);
    const pinnedRow = client
      .getQueryData<ConversationsModule.PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)
      ?.conversations.find((row) => row.id === session.id);
    expect(pinnedRow?.labels["omnigent.wrapper"]).toBeUndefined();
    expect(pinnedRow?.harness_override).toBe(session.harness);
    expect(
      fetchSpy.mock.calls.filter(([url]) => String(url).includes("/model-options")),
    ).toHaveLength(1);
    expect(fetchSpy.mock.calls.some(([url]) => String(url).startsWith("/v1/sessions?"))).toBe(
      false,
    );
    expect(client.getQueriesData({ queryKey: ["conversations"] })).toHaveLength(0);
    await act(async () =>
      resolvePatch(new Response(JSON.stringify({ ...wire, labels: { "omnigent.pinned": "123" } }))),
    );
    client.clear();
  });
});
