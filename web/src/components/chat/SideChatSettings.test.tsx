import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { useSession } from "@/hooks/useSession";
import * as runnerHealth from "@/hooks/RunnerHealthProvider";
import * as sessionsApi from "@/lib/sessionsApi";
import * as identity from "@/lib/identity";
import type { Session, NativeModelOption } from "@/lib/types";
import type * as AgentLabels from "@/lib/agentLabels";
import { conversationRegistry } from "@/store/conversationRegistry";
import { handleSessionEvent, useChatStore } from "@/store/chatStore";
import { SideChatSettings } from "./SideChatSettings";

vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabels>()),
  useHarnessEffortFamilies: () => ({
    "claude-sdk": "anthropic",
    codex: "openai",
    "antigravity-native": "gemini",
  }),
}));

const models: NativeModelOption[] = [
  {
    id: "model-a",
    displayName: "Model A",
    defaultReasoningEffort: "medium",
    supportedReasoningEfforts: [{ reasoningEffort: "medium" }, { reasoningEffort: "high" }],
  },
  {
    id: "model-b",
    displayName: "Model B",
    defaultReasoningEffort: "low",
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "max" }],
  },
];
const parent = {
  id: "parent",
  modelOverride: "model-a",
  reasoningEffort: "high",
  codexModelOptions: models,
} as Session;
const child = {
  agentId: "agent",
  agentName: "Devin",
  status: "idle",
  createdAt: 0,
  title: "Side chat",
  items: [],
  subAgentName: null,
  kind: "default",
  id: "side",
  parentSessionId: null,
  harness: "devin-native",
  codexModelOptions: models,
  permissionLevel: 4,
  modelOverride: "model-a",
  reasoningEffort: "high",
  labels: {
    "omnigent.wrapper": "devin-native-ui",
    "omnigent.side_chat": "1",
    "omnigent.side_chat.source_id": "parent",
  },
} as Session;
let cache: QueryClient;

function Pane() {
  const { session } = useSession("side");
  return session ? <SideChatSettings session={session} /> : null;
}

function mount(session = child) {
  cache.setQueryData(["session", "side"], session);
  cache.setQueryData(["session", "parent"], parent);
  return render(
    <QueryClientProvider client={cache}>
      <Pane />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  useChatStore.setState({ conversationId: "parent", sideChatComposers: {} });
  vi.spyOn(toast, "error").mockReturnValue("error");
  vi.spyOn(sessionsApi, "updateSession").mockImplementation(
    async (id, updates) =>
      ({ ...cache.getQueryData<Session>(["session", id]), ...updates }) as Session,
  );
});

afterEach(() => {
  cleanup();
  cache.clear();
  conversationRegistry.clear();
  vi.restoreAllMocks();
});

it("changes only the side session and uses the selected model's effort ladder", async () => {
  mount();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
      modelOverride: "model-b",
      reasoningEffort: null,
    }),
  );
  await waitFor(() => expect(screen.getByRole("menuitemcheckbox", { name: "Max" })).toBeEnabled());
  expect(screen.queryByRole("menuitemcheckbox", { name: "High" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Max" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenLastCalledWith("side", { reasoningEffort: "max" }),
  );
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
  expect(useChatStore.getState().conversationId).toBe("parent");
});

it("keeps the saved selection when the server rejects a change", async () => {
  vi.mocked(sessionsApi.updateSession).mockRejectedValue(new Error("Model unavailable"));
  mount();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Model unavailable");
  expect(cache.getQueryData(["session", "side"])).toEqual(child);
  expect(screen.getByRole("menuitemcheckbox", { name: "Model A" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps the reported Claude model when a saved request is not applied", async () => {
  conversationRegistry.acquire("side").setState({ llmModel: "model-a" });
  mount({
    ...child,
    harness: "claude-native",
    hostId: null,
    labels: { ...child.labels, "omnigent.wrapper": "claude-code-native-ui" },
  });
  const user = userEvent.setup();
  const picker = screen.getByRole("button", { name: "Side chat model and effort" });
  await user.click(picker);
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  await waitFor(() =>
    expect(cache.getQueryData<Session>(["session", "side"])?.modelOverride).toBe("model-b"),
  );
  act(() =>
    handleSessionEvent(
      {
        type: "error",
        source: "execution",
        toolName: null,
        error: { code: "model_change_not_applied", message: "The terminal was not switched." },
      },
      "side",
    ),
  );
  expect(picker).toHaveTextContent("Model A");
  expect(screen.getByRole("menuitemcheckbox", { name: "Model A" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByRole("menuitemcheckbox", { name: "Model B" })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  act(() =>
    handleSessionEvent({ type: "session_model", conversationId: "side", model: "model-b" }, "side"),
  );
  expect(picker).toHaveTextContent("Model B");
  expect(screen.getByRole("menuitemcheckbox", { name: "Model B" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

it("offers SDK effort controls without inventing a model catalog", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "claude-sdk",
    codexModelOptions: [],
    labels: {},
    modelOverride: "claude-sonnet",
    reasoningEffort: "high",
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.queryByTestId("side-chat-models")).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Low" }));
  expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", { reasoningEffort: "low" });
});

it("keeps compatible effort when changing a configured generic side chat model", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "codex",
    labels: {},
    inferenceConfigured: true,
    codexModelOptions: models,
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
      modelOverride: "model-b",
    }),
  );
  cleanup();
  render(
    <QueryClientProvider client={cache}>
      <Pane />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "Model BHigh",
  );
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
});

it("changes only the model on a harness without effort controls", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "cursor-native",
    labels: { "omnigent.wrapper": "cursor-native-ui" },
    codexModelOptions: models,
    reasoningEffort: null,
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.queryByTestId("side-chat-efforts")).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", { modelOverride: "model-b" });
});

it("does not present a catalog default as the side chat's inherited effort", () => {
  mount({ ...child, reasoningEffort: null });
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "Model A",
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).not.toHaveTextContent(
    /medium/i,
  );
});

it.each([1, 4])("disables settings for a viewer or a closed side chat", (permissionLevel) => {
  mount({
    ...child,
    permissionLevel,
    labels: { ...child.labels, ...(permissionLevel === 4 ? { "omnigent.closed": "true" } : {}) },
  });
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toBeDisabled();
});

function DraftPane({ id }: { id: string }) {
  const draft = useChatStore((state) => state.sideChatComposers[id]);
  return (
    <SideChatSettings
      session={{
        ...child,
        id: "parent",
        parentSessionId: null,
        codexModelOptions: models,
        labels: { "omnigent.wrapper": "devin-native-ui" },
      }}
      draft={{
        settings: draft?.settings,
        disabled: draft?.starting ?? false,
        onChange: (updates) =>
          useChatStore.getState().updateSideChatComposer(id, (current) => ({
            ...current,
            settings: { ...current.settings, ...updates },
          })),
      }}
    />
  );
}

it("does not pin a runner-reported model when changing only draft effort", async () => {
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={cache}>
      <SideChatSettings
        session={{
          ...child,
          id: "parent",
          parentSessionId: null,
          harness: "claude-native",
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          modelOverride: null,
          llmModel: "reported-model[1m]",
        }}
        draft={{ onChange, disabled: false }}
      />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Low" }));
  expect(onChange).toHaveBeenCalledWith({ reasoningEffort: "low" });
  expect(sessionsApi.updateSession).not.toHaveBeenCalled();
});

it("shows an explicit draft model ahead of the source Claude report", async () => {
  render(
    <QueryClientProvider client={cache}>
      <SideChatSettings
        session={{
          ...child,
          harness: "claude-native",
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          llmModel: "model-a",
        }}
        draft={{ settings: { modelOverride: "model-b" }, onChange: vi.fn(), disabled: false }}
      />
    </QueryClientProvider>,
  );
  const picker = screen.getByRole("button", { name: "Side chat model and effort" });
  expect(picker).toHaveTextContent("Model B");
  const user = userEvent.setup();
  await user.click(picker);
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.getByRole("menuitemcheckbox", { name: "Model B" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(sessionsApi.updateSession).not.toHaveBeenCalled();
});

it.each([false, true])(
  "clears stale effort for a model with no effort choices (draft=%s)",
  async (draft) => {
    const onChange = vi.fn();
    const session = {
      ...child,
      codexModelOptions: [
        models[0],
        { id: "model-without-effort", displayName: "No effort model" },
      ],
    };
    cache.setQueryData(["session", "side"], session);
    cache.setQueryData(["session", "parent"], parent);
    const view = render(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={session}
          draft={draft ? { onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
    await user.click(screen.getByTestId("side-chat-model-edit"));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "No effort model" }));
    const updates = { modelOverride: "model-without-effort", reasoningEffort: null };
    if (draft) expect(onChange).toHaveBeenCalledWith(updates);
    else expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", updates);
    await waitFor(() =>
      expect(screen.getByRole("menuitemcheckbox", { name: "No effort model" })).toBeEnabled(),
    );
    view.rerender(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={draft ? session : { ...session, ...updates }}
          draft={draft ? { settings: updates, onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Side chat model and effort" }),
    ).not.toHaveTextContent("High");
    expect(screen.queryByTestId("side-chat-efforts")).not.toBeInTheDocument();
  },
);

it("keeps unsent settings per draft across remounts without updating the parent", async () => {
  const user = userEvent.setup();
  const view = render(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:one" />
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  expect(useChatStore.getState().sideChatComposers["pending:one"].settings).toEqual({
    modelOverride: "model-b",
    reasoningEffort: null,
  });
  expect(sessionsApi.updateSession).not.toHaveBeenCalled();
  view.unmount();
  const remount = render(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:one" />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    /Model B$/,
  );
  act(() =>
    useChatStore
      .getState()
      .updateSideChatComposer("pending:one", (draft) => ({ ...draft, starting: true })),
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toBeDisabled();
  remount.rerender(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:two" />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "Model AHigh",
  );
});

it.each(["live", "draft", "nested-draft"] as const)(
  "uses the shared host catalog when session models are empty (%s)",
  async (mode) => {
    const draft = mode !== "live";
    const source = { ...parent, hostId: "host", codexModelOptions: [] };
    cache.setQueryData(["session", "parent"], source);
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockResolvedValue(new Response(JSON.stringify({ models }), { status: 200 }));
    const onChange = vi.fn();
    const session = draft
      ? {
          ...child,
          id: mode === "nested-draft" ? "side" : "parent",
          parentSessionId: null,
          hostId: mode === "nested-draft" ? null : "host",
          labels:
            mode === "nested-draft" ? child.labels : { "omnigent.wrapper": "devin-native-ui" },
          codexModelOptions: [],
        }
      : { ...child, codexModelOptions: [] };
    const view = render(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={session}
          draft={draft ? { onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Side chat model and effort" }));
    await user.click(screen.getByTestId("side-chat-model-edit"));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
    expect(fetch).toHaveBeenCalledWith("/v1/hosts/host/harnesses/devin-native/model-options");
    if (draft) {
      expect(onChange).toHaveBeenCalledWith({ modelOverride: "model-b", reasoningEffort: null });
      expect(sessionsApi.updateSession).not.toHaveBeenCalled();
    } else {
      expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
        modelOverride: "model-b",
        reasoningEffort: null,
      });
    }
    expect(cache.getQueryData(["session", "parent"])).toEqual(source);
    view.rerender(
      <QueryClientProvider client={cache}>
        <SideChatSettings session={{ ...session, codexModelOptions: [models[0]] }} />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("menuitemcheckbox", { name: "Model B" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "Model A" })).toBeInTheDocument();
  },
);

it.each([
  { harness: "codex-native", labels: {} },
  { harness: null, labels: { "omnigent.wrapper": "codex-native-ui" } },
  { harness: null, labels: { "omnigent.codex_native.agent_nickname": "Side chat" } },
])("hides native Codex settings when unsupported (%j)", (metadata) => {
  mount({ ...child, ...metadata } as Session);
  expect(screen.queryByRole("button", { name: "Side chat model and effort" })).toBeNull();
});

it.each([false, true])("hides native settings before a child exists (draft=%s)", (draft) => {
  render(
    <QueryClientProvider client={cache}>
      <SideChatSettings
        session={{
          ...parent,
          sideChatSettingsSupported: true,
          harness: "codex-native",
          labels: { "omnigent.wrapper": "codex-native-ui" },
        }}
        draft={draft ? { onChange: vi.fn(), disabled: false } : undefined}
      />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("button", { name: "Side chat model and effort" })).toBeNull();
});

it.each([false, true])(
  "preserves unspecified effort when selecting a model (draft=%s)",
  async (draft) => {
    const onChange = vi.fn();
    const session = {
      ...child,
      harness: "claude-native",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      reasoningEffort: null,
      codexModelOptions: [models[0], { ...models[1], defaultReasoningEffort: "unsupported" }],
    };
    render(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={session}
          draft={draft ? { onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
    await user.click(screen.getByTestId("side-chat-model-edit"));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
    const updates = { modelOverride: "model-b" };
    if (draft) expect(onChange).toHaveBeenCalledWith(updates);
    else expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", updates);
  },
);

it("hides unsupported custom native effort", () => {
  render(
    <QueryClientProvider client={cache}>
      <SideChatSettings
        session={{
          ...child,
          harness: "antigravity-native",
          labels: { "omnigent.ui": "terminal" },
          codexModelOptions: [],
        }}
      />
    </QueryClientProvider>,
  );
  expect(
    screen.queryByRole("button", { name: "Side chat model and effort" }),
  ).not.toBeInTheDocument();
});

it.each([
  { draft: false, model: "fusion-lead-high-sidekick-helper" },
  { draft: true, model: "fusion-lead-high-sidekick-helper" },
  { draft: false, model: "model-a" },
  { draft: true, model: "model-a" },
])(
  "selects a runnable Devin Fusion pairing (draft=$draft, current=$model)",
  async ({ draft, model }) => {
    const inherited = model.startsWith("fusion-");
    const onChange = vi.fn();
    const session = {
      ...child,
      modelOverride: model,
      reasoningEffort: inherited ? null : "high",
      codexModelOptions: [
        models[0],
        {
          id: "fusion",
          displayName: "Fusion",
          fusion: {
            default: "fusion-lead-medium-sidekick-helper",
            combos: ["medium", "high"].map((effort) => ({
              modelUid: `fusion-lead-${effort}-sidekick-helper`,
              lead: "lead",
              leadLabel: "Lead",
              effort,
              fast: false,
              sidekick: "helper",
              sidekickLabel: "Helper",
              priority: false,
            })),
          },
        },
      ],
    };
    render(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={session}
          draft={draft ? { onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    const picker = screen.getByRole("button", { name: "Side chat model and effort" });
    expect(picker).toHaveTextContent(inherited ? "Fusion · Lead High + Helper" : "Model A");
    const user = userEvent.setup();
    await user.click(picker);
    await user.click(screen.getByTestId("side-chat-model-edit"));
    const fusion = screen.getByRole("menuitemcheckbox", { name: "Fusion" });
    expect(fusion).toHaveAttribute("aria-checked", String(inherited));
    if (inherited) expect(screen.queryByTestId("side-chat-efforts")).not.toBeInTheDocument();
    await user.click(fusion);
    const updates = inherited
      ? { modelOverride: model }
      : { modelOverride: "fusion-lead-medium-sidekick-helper", reasoningEffort: null };
    if (draft) expect(onChange).toHaveBeenCalledWith(updates);
    else expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", updates);
  },
);

it("uses the parent catalog while targeting a native Codex side thread", async () => {
  mount({
    ...child,
    harness: "codex-native",
    sideChatSettingsSupported: true,
    parentSessionId: "parent",
    codexModelOptions: [],
    labels: {
      "omnigent.wrapper": "codex-native-ui-subagent",
      "omnigent.codex_native.agent_nickname": "Side chat",
    },
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Model B" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
      modelOverride: "model-b",
      reasoningEffort: null,
    }),
  );
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
});

it("refreshes native settings support when the parent runner reconnects", async () => {
  const session = {
    ...child,
    harness: "codex-native",
    sideChatSettingsSupported: false,
    parentSessionId: "parent",
    labels: {
      "omnigent.wrapper": "codex-native-ui-subagent",
      "omnigent.codex_native.agent_nickname": "Side chat",
    },
  };
  const health = vi
    .spyOn(runnerHealth, "useRunnerHealthRegistration")
    .mockReturnValue(new Map([["parent", false]]));
  const snapshot = vi.spyOn(sessionsApi, "getSessionSlim").mockResolvedValue(session);
  const view = mount(session);
  await waitFor(() => expect(snapshot).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("button", { name: "Side chat model and effort" })).toBeNull();

  snapshot.mockResolvedValue({ ...session, sideChatSettingsSupported: true });
  health.mockReturnValue(new Map([["parent", true]]));
  view.rerender(
    <QueryClientProvider client={cache}>
      <Pane />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("button", { name: "Side chat model and effort" })).toBeVisible();
  expect(snapshot).toHaveBeenCalledTimes(2);
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
});
