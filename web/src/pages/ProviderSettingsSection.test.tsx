import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProviderSettingsSection } from "./ProviderSettingsSection";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { useHosts, useInstallHarness, type Host } from "@/hooks/useHosts";
import {
  detectSetup,
  fetchSetupOperation,
  fetchSetupInventory,
  runSetupAction,
  startSetupOperation,
  type SetupInventory,
  type SetupOperation,
} from "@/lib/providerSetupApi";

vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useInstallHarness: vi.fn(),
}));
vi.mock("@/lib/providerSetupApi", () => ({
  detectSetup: vi.fn(),
  fetchSetupOperation: vi.fn(),
  fetchSetupInventory: vi.fn(),
  runSetupAction: vi.fn(),
  startSetupOperation: vi.fn(),
}));
vi.mock("@/components/ProviderSetupTerminal", () => ({
  ProviderSetupTerminal: ({
    operation,
    onOperationChange,
  }: {
    operation: SetupOperation;
    onOperationChange: (operation: SetupOperation) => void;
  }) => (
    <div data-testid="setup-terminal" data-operation-id={operation.operation_id}>
      <button type="button" onClick={() => onOperationChange({ ...operation })}>
        Simulate operation poll
      </button>
      <button type="button" onClick={() => onOperationChange({ ...operation, state: "succeeded" })}>
        Simulate operation completion
      </button>
      <button type="button" onClick={() => onOperationChange({ ...operation, state: "running" })}>
        Simulate stale running poll
      </button>
      <button
        type="button"
        onClick={() => {
          deliverDelayedCancellation = () =>
            onOperationChange({ ...operation, state: "cancelled" });
        }}
      >
        Simulate delayed cancellation
      </button>
    </div>
  ),
}));
vi.mock("@/shell/HarnessSetupDialog", () => ({
  HarnessSetupDialog: () => null,
}));
// Radix Select uses portals and pointer events that are incidental to these
// host-scoping tests. A native select keeps each option and payload path easy
// to exercise in jsdom.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange: (next: string) => void;
    children: ReactNode;
  }) => (
    <select
      data-testid="mock-select"
      value={value ?? ""}
      onChange={(event) => onValueChange(event.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => children,
  SelectItem: ({
    value,
    disabled,
    children,
  }: {
    value: string;
    disabled?: boolean;
    children: ReactNode;
  }) => (
    <option value={value} disabled={disabled}>
      {children}
    </option>
  ),
}));

const useHostsMock = vi.mocked(useHosts);
const fetchInventoryMock = vi.mocked(fetchSetupInventory);
const fetchSetupOperationMock = vi.mocked(fetchSetupOperation);
const detectSetupMock = vi.mocked(detectSetup);
const runSetupActionMock = vi.mocked(runSetupAction);
const startSetupOperationMock = vi.mocked(startSetupOperation);
const scrollIntoViewMock = vi.fn();
const installHarnessMock = vi.fn();
let deliverDelayedCancellation: (() => void) | undefined;

const online = (host_id: string, name = host_id): Host => ({
  host_id,
  name,
  owner: "jakob@example.com",
  status: "online",
});

const offline = (host_id: string, name = host_id): Host => ({
  ...online(host_id, name),
  status: "offline",
});

function inventory(overrides: Partial<SetupInventory> = {}): SetupInventory {
  return {
    feature_enabled: true,
    mutations_enabled: true,
    providers: [],
    key_providers: [{ id: "openai", label: "OpenAI", family: "openai", base_url: "" }],
    acp_agents: [],
    harness_settings: {
      cursor_key_configured: false,
      antigravity_key_configured: false,
      copilot_key_configured: false,
    },
    dismissed_detections: [],
    effective_defaults: {},
    supported_operations: ["claude-login", "codex-login"],
    ...overrides,
  };
}

function enabledInfo(enabled = true) {
  return {
    ...FALLBACK_SERVER_INFO,
    features: { harness_install: enabled },
    installable_harnesses: ["claude-native", "codex-native"],
  };
}

function renderSection(featureEnabled = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={enabledInfo(featureEnabled)}>
        <ProviderSettingsSection />
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

function hostSelect(): HTMLSelectElement {
  return screen.getAllByTestId("mock-select")[0] as HTMLSelectElement;
}

async function openAdvanced(task: string) {
  fireEvent.click(await screen.findByText("Advanced provider tools"));
  fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${task}`) }));
}

async function openAgent(id: string) {
  if (!["claude", "codex", "cursor", "opencode", "pi"].includes(id)) {
    fireEvent.click(await screen.findByRole("button", { name: "More agents" }));
  }
  fireEvent.click(await screen.findByTestId(`setup-agent-${id}`));
}

function actionResult(hostId: string, message = "Saved") {
  return { ok: true, message, inventory: inventories.get(hostId) ?? inventory() };
}

let hosts: Host[] | undefined;
let inventories: Map<string, SetupInventory>;

beforeEach(() => {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: scrollIntoViewMock,
  });
  localStorage.clear();
  sessionStorage.clear();
  hosts = [];
  inventories = new Map();
  useHostsMock.mockReset();
  fetchInventoryMock.mockReset();
  fetchSetupOperationMock.mockReset();
  detectSetupMock.mockReset();
  runSetupActionMock.mockReset();
  startSetupOperationMock.mockReset();
  scrollIntoViewMock.mockReset();
  installHarnessMock.mockReset();
  deliverDelayedCancellation = undefined;
  vi.mocked(useInstallHarness).mockReturnValue({
    mutate: installHarnessMock,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useInstallHarness>);
  useHostsMock.mockImplementation(
    () =>
      ({
        data: hosts,
        isPending: false,
        isError: false,
        refetch: vi.fn(),
      }) as unknown as ReturnType<typeof useHosts>,
  );
  fetchInventoryMock.mockImplementation(async (hostId) => inventories.get(hostId) ?? inventory());
  detectSetupMock.mockResolvedValue({ providers: [], imports: [], models: {} });
  runSetupActionMock.mockImplementation(async (hostId) => actionResult(hostId));
  startSetupOperationMock.mockResolvedValue({
    operation_id: "op-1",
    state: "running",
    action: "codex-login",
    exit_code: null,
    error: null,
  });
  fetchSetupOperationMock.mockResolvedValue({
    operation_id: "op-1",
    state: "running",
    action: "codex-login",
    exit_code: null,
    error: null,
  });
});

afterEach(() => cleanup());

describe("ProviderSettingsSection", () => {
  it("shows one agent list and opens a scoped Codex key form in the first preview", async () => {
    hosts = [online("mac", "Mac")];
    inventories.set("mac", inventory());
    renderSection();

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    for (const label of ["Codex", "Cursor", "OpenCode", "Pi"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("Advanced provider tools").closest("details")).not.toHaveAttribute(
      "open",
    );
    fireEvent.click(screen.getByText("Codex"));
    expect(screen.getByRole("button", { name: "Back to agents" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "API key" }));
    expect(detectSetupMock).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "test-only-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_key",
        provider: "openai",
        name: undefined,
        secret: "test-only-key",
      }),
    );
    expect(screen.queryByLabelText("API key")).toBeNull();
  });

  it("keeps a started Codex sign-in inside its agent detail", async () => {
    hosts = [{ ...online("mac", "Mac"), configured_harnesses: { "codex-native": false } }];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    renderSection();

    fireEvent.click(await screen.findByText("Codex"));
    expect(screen.queryByText("Install Codex")).toBeNull();
    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await waitFor(() =>
      expect(startSetupOperationMock).toHaveBeenCalledWith("mac", "codex-login", {}),
    );
    expect(await screen.findByTestId("setup-terminal")).toBeInTheDocument();
    expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBe("op-1");
  });

  it("blocks sign-in for an outdated CLI even when an operation is advertised", async () => {
    hosts = [
      { ...online("mac", "Mac"), configured_harnesses: { "codex-native": "version-too-low" } },
    ];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    renderSection();

    await openAgent("codex");
    expect(screen.getByText("Update Codex")).toBeInTheDocument();
    expect(
      screen.getByText(/Update Codex on Mac, then check setup status again/),
    ).toBeInTheDocument();
    const signIn = screen.getByRole("button", { name: "ChatGPT subscription" });
    expect(signIn).toBeDisabled();
    fireEvent.click(signIn);
    expect(startSetupOperationMock).not.toHaveBeenCalled();
  });

  it("offers Pi's existing gateway and scopes Databricks to Pi across reload", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["databricks-configure"] }));
    startSetupOperationMock.mockResolvedValue({
      operation_id: "op-pi",
      state: "running",
      action: "databricks-configure",
      exit_code: null,
      error: null,
    });
    const first = renderSection();

    await openAgent("pi");
    expect(screen.getByRole("button", { name: "Compatible gateway" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Databricks" }));
    expect(screen.getByLabelText("Configure for")).toHaveValue("current");
    expect(within(screen.getByLabelText("Configure for")).getAllByRole("option")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Workspace URL"), {
      target: { value: "https://workspace.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^configure$/i }));
    await waitFor(() =>
      expect(startSetupOperationMock).toHaveBeenCalledWith("mac", "databricks-configure", {
        workspace_url: "https://workspace.example.com",
        agents: ["pi"],
      }),
    );
    expect(screen.getByTestId("setup-terminal")).toBeInTheDocument();
    expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac:agent")).toBe("pi");

    first.unmount();
    fetchSetupOperationMock.mockResolvedValue({
      operation_id: "op-pi",
      state: "running",
      action: "databricks-configure",
      exit_code: null,
      error: null,
    });
    renderSection();
    expect(await screen.findByTestId("setup-terminal")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pi" })).toBeInTheDocument();
  });

  it("keeps Claude, Codex, and Bedrock connections out of Pi's saved defaults", async () => {
    hosts = [online("mac", "Mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "claude-subscription",
            kind: "subscription",
            families: ["anthropic"],
            defaults: ["anthropic"],
            default_scopes: ["anthropic"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
          {
            name: "codex-subscription",
            kind: "subscription",
            families: ["openai"],
            defaults: ["openai"],
            default_scopes: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
          {
            name: "bedrock",
            kind: "bedrock",
            families: ["anthropic"],
            defaults: [],
            default_scopes: ["anthropic"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
        effective_defaults: {
          anthropic: "claude-subscription",
          openai: "codex-subscription",
          pi: null,
        },
      }),
    );
    renderSection();

    expect(await screen.findByTestId("setup-agent-pi")).toHaveTextContent("Choose how to connect");
    await openAgent("pi");
    expect(screen.queryByText("Saved connections")).toBeNull();
    expect(screen.queryByText("Used for new sessions")).toBeNull();
    expect(screen.queryByRole("button", { name: /Use for new Pi sessions/ })).toBeNull();
    const localConfiguration = screen.getByRole("button", {
      name: "Use Pi’s local configuration",
    });
    expect(localConfiguration).toBeInTheDocument();
    expect(screen.getByText(/does not check your Pi sign-in/)).toBeInTheDocument();
    fireEvent.click(localConfiguration);
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "subscription",
        cli: "pi",
      }),
    );
    expect(startSetupOperationMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("claude");
    expect(screen.getByTestId("agent-provider-row-claude-subscription")).toHaveTextContent(
      "Used for new sessions",
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("codex");
    expect(screen.getByTestId("agent-provider-row-codex-subscription")).toHaveTextContent(
      "Used for new sessions",
    );
  });

  it("does not infer Pi compatibility when the host reports no default scopes", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "unavailable-key",
            kind: "key",
            families: ["openai"],
            defaults: [],
            default_scopes: [],
            credential_sources: { openai: "stored" },
            models: {},
            base_urls: {},
          },
        ],
      }),
    );
    renderSection();
    await openAgent("pi");
    expect(screen.queryByText("unavailable-key")).toBeNull();
    expect(screen.queryByRole("button", { name: "Use for new Pi sessions" })).toBeNull();
  });

  it("drops an unsupported default scope when a connection is replaced", async () => {
    hosts = [online("mac")];
    const provider = {
      name: "gateway",
      kind: "gateway",
      families: ["anthropic", "openai"],
      defaults: [],
      default_scopes: ["anthropic", "openai", "pi"],
      credential_sources: {},
      models: {},
      base_urls: {},
    };
    inventories.set("mac", inventory({ providers: [provider] }));
    const { client } = renderSection();
    await openAdvanced("Manage all connections");
    const row = screen.getByTestId("provider-row-gateway");
    fireEvent.change(within(row).getByTestId("mock-select"), { target: { value: "openai" } });
    const replacement = inventory({
      providers: [{ ...provider, families: ["anthropic"], default_scopes: ["anthropic", "pi"] }],
    });
    inventories.set("mac", replacement);
    await act(async () => {
      client.setQueryData(["provider-setup", "mac"], replacement);
    });
    await waitFor(() => expect(within(row).getByTestId("mock-select")).toHaveValue("anthropic"));
    fireEvent.click(within(row).getByRole("button", { name: "Make default" }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_default",
        name: "gateway",
        surface: "anthropic",
      }),
    );
  });

  it("checks Pi's inherited default explicitly and clears it after a failed recheck", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "codex-config",
            kind: "cli-config",
            families: ["openai", "pi"],
            default_scopes: ["openai", "pi"],
            defaults: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
        effective_defaults: { openai: "codex-config", pi: null },
        pi_default_requires_detection: true,
      }),
    );
    detectSetupMock
      .mockResolvedValueOnce({
        providers: [],
        imports: [],
        models: {},
        pi_default_checked: true,
        pi_default_provider: "codex-config",
      })
      .mockRejectedValueOnce(new Error("Host unavailable"));
    renderSection();
    await openAgent("pi");
    const row = screen.getByTestId("agent-provider-row-codex-config");
    expect(row).not.toHaveTextContent("Used for new sessions");
    expect(detectSetupMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Check Pi default" }));
    await waitFor(() => expect(row).toHaveTextContent("Used for new sessions"));
    expect(detectSetupMock).toHaveBeenCalledWith("mac", { pi_default: true });
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Host unavailable");
    expect(row).not.toHaveTextContent("Used for new sessions");
    expect(screen.getByRole("button", { name: "Check Pi default" })).toBeEnabled();
  });

  it("discards a late Pi default result after the saved inventory changes", async () => {
    hosts = [online("mac")];
    const original = inventory({
      providers: [
        {
          name: "codex-config",
          kind: "cli-config",
          families: ["openai", "pi"],
          default_scopes: ["openai", "pi"],
          defaults: ["openai"],
          credential_sources: {},
          models: {},
          base_urls: {},
        },
      ],
      effective_defaults: { openai: "codex-config", pi: null },
      pi_default_requires_detection: true,
    });
    inventories.set("mac", original);
    let resolve!: (result: Awaited<ReturnType<typeof detectSetup>>) => void;
    detectSetupMock.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const { client } = renderSection();
    await openAgent("pi");
    fireEvent.click(screen.getByRole("button", { name: "Check Pi default" }));
    await waitFor(() => expect(detectSetupMock).toHaveBeenCalled());
    const updated = { ...original, dismissed_detections: ["changed"] };
    await act(async () => {
      client.setQueryData(["provider-setup", "mac"], updated);
    });
    await act(async () => {
      resolve({
        providers: [],
        imports: [],
        models: {},
        pi_default_checked: true,
        pi_default_provider: "codex-config",
      });
    });
    expect(screen.getByTestId("agent-provider-row-codex-config")).not.toHaveTextContent(
      "Used for new sessions",
    );
    expect(screen.getByRole("button", { name: "Check Pi default" })).toBeEnabled();
  });

  it("uses the Pi scope for its default action while advanced keeps its scope picker", async () => {
    hosts = [online("mac", "Mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "pi-subscription",
            kind: "subscription",
            families: [],
            defaults: ["pi"],
            default_scopes: ["pi"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
          {
            name: "dual-gateway",
            kind: "gateway",
            families: ["anthropic", "openai"],
            defaults: ["openai"],
            default_scopes: ["anthropic", "openai", "pi"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
        effective_defaults: { pi: "pi-subscription", openai: "dual-gateway" },
      }),
    );
    renderSection();

    expect(await screen.findByTestId("setup-agent-pi")).toHaveTextContent("2 saved connections");
    await openAgent("pi");
    expect(screen.getByTestId("agent-provider-row-pi-subscription")).toHaveTextContent(
      "Used for new sessions",
    );
    const gateway = screen.getByTestId("agent-provider-row-dual-gateway");
    expect(gateway).not.toHaveTextContent("Used for new sessions");
    expect(
      screen.getByText(/Changing the default affects new Pi sessions on Mac/),
    ).toBeInTheDocument();
    fireEvent.click(within(gateway).getByRole("button", { name: "Use for new Pi sessions" }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_default",
        name: "dual-gateway",
        surface: "pi",
      }),
    );

    await openAdvanced("Manage all connections");
    const advancedGateway = screen.getByTestId("provider-row-dual-gateway");
    fireEvent.change(within(advancedGateway).getByTestId("mock-select"), {
      target: { value: "anthropic" },
    });
    fireEvent.click(within(advancedGateway).getByRole("button", { name: "Make default" }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_default",
        name: "dual-gateway",
        surface: "anthropic",
      }),
    );
  });

  it("keeps both Anthropic and OpenAI API-key vendors available to Pi", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        key_providers: [
          { id: "anthropic", label: "Anthropic", family: "anthropic", base_url: "" },
          { id: "openai", label: "OpenAI", family: "openai", base_url: "" },
        ],
      }),
    );
    renderSection();

    await openAgent("pi");
    fireEvent.click(screen.getByRole("button", { name: "API key" }));
    expect(
      within(screen.getByLabelText("Vendor")).getByRole("option", { name: "Anthropic" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Vendor")).getByRole("option", { name: "OpenAI" }),
    ).toBeInTheDocument();
  });

  it("scopes detection results and Claude's Keychain notice to relevant agents", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    detectSetupMock.mockResolvedValue({
      providers: [
        { name: "claude-login", kind: "subscription", family: "anthropic", source: "fixture" },
        { name: "codex-login", kind: "subscription", family: "openai", source: "fixture" },
        { name: "pi-login", kind: "subscription", family: "pi", source: "fixture" },
        { name: "anthropic-key", kind: "key", family: "anthropic", source: "fixture" },
        { name: "openai-gateway", kind: "gateway", family: "openai", source: "fixture" },
        { name: "bedrock", kind: "bedrock", family: "anthropic", source: "fixture" },
      ],
      imports: [],
      models: {},
      warnings: [
        "Claude logins stored only in the OS Keychain are checked through guided sign-in, not detection.",
        "Some host connections could not be inspected",
      ],
    });
    renderSection();

    await openAgent("pi");
    fireEvent.click(screen.getByRole("button", { name: "Find credentials on this computer" }));
    const piResults = await screen.findByTestId("provider-detection-results");
    for (const name of ["pi-login", "anthropic-key", "openai-gateway"]) {
      expect(within(piResults).getByText(name)).toBeInTheDocument();
    }
    for (const name of ["claude-login", "codex-login", "bedrock"]) {
      expect(within(piResults).queryByText(name)).toBeNull();
    }
    expect(within(piResults).queryByText(/Claude logins stored only/)).toBeNull();
    expect(
      within(piResults).getByText("Some host connections could not be inspected"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("claude");
    const claudeResults = screen.getByTestId("provider-detection-results");
    expect(within(claudeResults).getByText(/Claude logins stored only/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("opencode");
    expect(screen.queryByRole("button", { name: "Find credentials on this computer" })).toBeNull();
    expect(screen.queryByTestId("provider-detection-results")).toBeNull();
  });

  it("uses Antigravity native readiness instead of the SDK's availability", async () => {
    hosts = [
      {
        ...online("mac"),
        configured_harnesses: { antigravity: true, "antigravity-native": "needs-auth" },
      },
    ];
    inventories.set("mac", inventory());
    renderSection();

    await openAgent("antigravity");
    expect(screen.getByText("Sign-in needed")).toBeInTheDocument();
    expect(screen.queryByText("Ready on this computer")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in to Antigravity" })).toBeInTheDocument();
  });

  it("shows an already verified Antigravity connection without opening a terminal", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["antigravity-login"] }));
    startSetupOperationMock.mockResolvedValue({
      operation_id: "op-antigravity",
      state: "succeeded",
      action: "antigravity-login",
      exit_code: null,
      error: null,
      already_connected: true,
    });
    renderSection();

    await openAgent("antigravity");
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Antigravity" }));

    expect(await screen.findByText(/Antigravity is already signed in/i)).toBeInTheDocument();
    expect(screen.queryByTestId("setup-terminal")).toBeNull();
  });

  it("keeps an active connection discoverable after choosing another agent", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["antigravity-login"] }));
    startSetupOperationMock.mockResolvedValue({
      operation_id: "op-antigravity",
      state: "running",
      action: "antigravity-login",
      exit_code: null,
      error: null,
      can_verify: true,
    });
    renderSection();

    await openAgent("antigravity");
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Antigravity" }));
    await screen.findByTestId("setup-terminal");
    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("codex");

    expect(screen.getByText("Antigravity connection in progress.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Return to Antigravity" }));
    expect(await screen.findByTestId("setup-terminal")).toBeInTheDocument();
  });

  it("does not claim a vendor sign-in when harness status is missing", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAgent("codex");
    expect(screen.getByText("Sign-in status not checked")).toBeInTheDocument();
  });

  it("checks only the selected native harness on the selected computer", async () => {
    hosts = [online("mac", "Mac"), online("linux", "Linux")];
    localStorage.setItem("omnigent:provider-settings-host", "mac");
    inventories.set("mac", inventory());
    inventories.set("linux", inventory());
    detectSetupMock.mockImplementation(async (_hostId, request) => ({
      providers: [],
      imports: [],
      models: {},
      harness_status: { harness: request?.harness ?? "antigravity-native", availability: true },
    }));
    renderSection();

    await openAgent("antigravity");
    expect(detectSetupMock).not.toHaveBeenCalled();
    expect(screen.getByText("Sign-in status not checked")).toBeInTheDocument();
    expect(screen.getByText(/It reads local CLI setup/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await waitFor(() =>
      expect(detectSetupMock).toHaveBeenCalledWith("mac", { harness: "antigravity-native" }),
    );
    expect(await screen.findByText("Ready according to setup")).toBeInTheDocument();

    fireEvent.change(hostSelect(), { target: { value: "linux" } });
    await openAgent("antigravity");
    expect(screen.getByText("Sign-in status not checked")).toBeInTheDocument();
    expect(detectSetupMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    await waitFor(() =>
      expect(detectSetupMock).toHaveBeenCalledWith("linux", { harness: "antigravity-native" }),
    );
    expect(await screen.findByText("Ready according to setup")).toBeInTheDocument();
  });

  it("keeps credential discovery after a status check and clears failed checked status", async () => {
    hosts = [{ ...online("mac"), configured_harnesses: { "codex-native": true } }];
    inventories.set("mac", inventory());
    detectSetupMock
      .mockResolvedValueOnce({
        providers: [{ name: "openai-key", kind: "key", family: "openai", source: "fixture" }],
        imports: [],
        models: {},
      })
      .mockResolvedValueOnce({
        providers: [],
        imports: [],
        models: {},
        harness_status: { harness: "codex-native", availability: true },
      })
      .mockResolvedValueOnce({
        providers: [],
        imports: [],
        models: {},
        warnings: ["The requested harness status could not be checked on this computer"],
      });
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "Find credentials on this computer" }));
    expect(await screen.findByText("openai-key")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText("Ready according to setup")).toBeInTheDocument();
    await waitFor(() => expect(fetchInventoryMock).toHaveBeenCalledTimes(2));
    expect(screen.getByText("openai-key")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    expect(
      await screen.findByText("The requested harness status could not be checked on this computer"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Ready according to setup")).toBeNull();
    expect(screen.queryByText("Available on this computer")).toBeNull();
    expect(screen.getByText("Status check failed")).toBeInTheDocument();
    expect(screen.getByText("openai-key")).toBeInTheDocument();
  });

  it.each([
    [false, "Installation needed"],
    ["binary-missing", "Installation needed"],
    ["needs-auth", "Sign-in or configuration needed"],
    ["version-too-low", "Update needed"],
  ] as const)("shows a checked %s setup result as %s", async (availability, label) => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    detectSetupMock.mockResolvedValue({
      providers: [],
      imports: [],
      models: {},
      harness_status: { harness: "codex-native", availability },
    });
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText(`${label}`)).toBeInTheDocument();
  });

  it.each([
    ["binary-missing", "Installation needed"],
    ["version-too-low", "Update needed"],
  ] as const)(
    "prioritizes %s readiness over a saved Codex connection",
    async (availability, label) => {
      hosts = [{ ...online("mac"), configured_harnesses: { "codex-native": availability } }];
      inventories.set(
        "mac",
        inventory({
          providers: [
            {
              name: "openai-key",
              kind: "key",
              families: ["openai"],
              defaults: [],
              default_scopes: ["openai"],
              credential_sources: {},
              models: {},
              base_urls: {},
            },
          ],
          supported_operations: [],
        }),
      );
      renderSection();

      const codex = await screen.findByTestId("setup-agent-codex");
      expect(codex).toHaveTextContent(label);
      expect(codex).not.toHaveTextContent("saved connection");
    },
  );

  it("discards checked readiness when setup changes or a guided sign-in starts", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "openai-key",
            kind: "key",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai", "pi"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
      }),
    );
    detectSetupMock.mockResolvedValue({
      providers: [],
      imports: [],
      models: {},
      harness_status: { harness: "codex-native", availability: true },
    });
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText("Ready according to setup")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use for new Codex sessions" }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_default",
        name: "openai-key",
        surface: "openai",
      }),
    );
    expect(screen.queryByText("Ready according to setup")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    expect(await screen.findByText("Ready according to setup")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await waitFor(() => expect(startSetupOperationMock).toHaveBeenCalled());
    expect(screen.queryByText("Ready according to setup")).toBeNull();
  });

  it("reveals only the selected advanced task and the host's built-in ACP instructions", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        builtin_acp: [
          {
            id: "devin",
            label: "Devin",
            install_command: "Install Devin CLI from the vendor",
            auth_instructions: "Run devin auth login",
          },
        ],
      }),
    );
    renderSection();

    fireEvent.click(await screen.findByText("Advanced provider tools"));
    expect(screen.queryByRole("button", { name: "Save host" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Import ACP agents/ }));
    expect(screen.getByRole("button", { name: "Preview import" })).toBeInTheDocument();
    expect(detectSetup).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Find existing configuration" }));
    await waitFor(() => expect(detectSetup).toHaveBeenCalledWith("mac", undefined));
    expect(
      await screen.findByText("No importable agents found. You can try a configuration path."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add provider" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back to tools" }));
    fireEvent.click(screen.getByRole("button", { name: /^Add a provider/ }));
    expect(screen.getByRole("button", { name: "Add provider" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Preview import" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "More agents" }));
    fireEvent.click(screen.getByTestId("setup-agent-devin"));
    expect(screen.getByText("Install Devin CLI from the vendor")).toBeInTheDocument();
    expect(screen.getByText("Run devin auth login")).toBeInTheDocument();
  });

  it("explains when a host cannot offer an installed agent's sign-in command", async () => {
    hosts = [{ ...online("mac", "Mac"), configured_harnesses: { "claude-native": "needs-auth" } }];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    renderSection();

    fireEvent.click(await screen.findByText("Claude Code"));
    expect(screen.getByRole("button", { name: "Claude subscription" })).toBeDisabled();
    expect(screen.getByText(/Claude Code setup command is unavailable on Mac/)).toBeInTheDocument();
    expect(screen.queryByText("Install Claude Code")).toBeNull();
  });

  it("selects the sole online computer and renders its inventory", async () => {
    hosts = [online("mac", "Jakob's Mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "work-openai",
            kind: "api_key",
            families: ["openai"],
            defaults: ["openai"],
            default_scopes: ["openai"],
            credential_sources: { openai: "keychain" },
            models: { openai: "gpt-5.6" },
            base_urls: {},
          },
        ],
        effective_defaults: { openai: "work-openai" },
      }),
    );

    renderSection();

    await openAdvanced("Manage all connections");
    expect(await screen.findByText("work-openai")).toBeInTheDocument();
    expect(fetchInventoryMock).toHaveBeenCalledWith("mac", expect.any(AbortSignal));
    expect(hostSelect().value).toBe("mac");
    expect(screen.getByText(/gpt-5\.6/)).toBeInTheDocument();
  });

  it("requires an explicit selection with several computers, but restores a remembered one", async () => {
    hosts = [online("mac", "Mac"), online("linux", "Linux")];
    inventories.set("linux", inventory());
    renderSection();

    expect(screen.getByText(/Choose a computer\. Omnigent will never move/)).toBeInTheDocument();
    expect(fetchInventoryMock).not.toHaveBeenCalled();

    fireEvent.change(hostSelect(), { target: { value: "linux" } });
    await waitFor(() =>
      expect(fetchInventoryMock).toHaveBeenCalledWith("linux", expect.any(AbortSignal)),
    );
    expect(localStorage.getItem("omnigent:provider-settings-host")).toBe("linux");

    cleanup();
    fetchInventoryMock.mockClear();
    localStorage.setItem("omnigent:provider-settings-host", "linux");
    localStorage.setItem("omnigent:provider-settings-host:name", "Linux");
    renderSection();
    await waitFor(() =>
      expect(fetchInventoryMock).toHaveBeenCalledWith("linux", expect.any(AbortSignal)),
    );
  });

  it("does not retarget a missing remembered computer", async () => {
    localStorage.setItem("omnigent:provider-settings-host", "gone");
    localStorage.setItem("omnigent:provider-settings-host:name", "Old laptop");
    hosts = [online("mac", "Mac")];
    renderSection();

    expect(await screen.findByText(/Old laptop is unavailable/)).toBeInTheDocument();
    expect(fetchInventoryMock).not.toHaveBeenCalled();
    expect(hostSelect().value).toBe("gone");
  });

  it("does not fetch setup inventory or expose mutations for an offline computer", async () => {
    hosts = [offline("mac", "Mac")];
    renderSection();

    expect(await screen.findByRole("status")).toHaveTextContent("Mac is offline");
    expect(fetchInventoryMock).not.toHaveBeenCalled();
    expect(runSetupActionMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /detect credentials/i })).toBeNull();
  });

  it("keeps the configuration overview read-only when harness install is disabled", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "read-only-provider",
            kind: "api_key",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai"],
            credential_sources: { openai: "environment" },
            models: { openai: "gpt-5.6" },
            base_urls: {},
          },
        ],
      }),
    );
    renderSection(false);

    await openAdvanced("Manage all connections");
    expect(await screen.findByText("read-only-provider")).toBeInTheDocument();
    expect(screen.getByText(/Provider setup changes are disabled/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /detect credentials|add provider|remove/i }),
    ).toBeNull();
    expect(runSetupActionMock).not.toHaveBeenCalled();
  });

  it("runs credential detection only after the explicit button click", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAdvanced("Manage all connections");
    expect(detectSetupMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /detect credentials/i }));
    await waitFor(() => expect(detectSetupMock).toHaveBeenCalledWith("mac", undefined));
  });

  it("requires a new selection when an import preview changes its command", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    const preview = (fingerprint: string) => ({
      providers: [],
      models: {},
      imports: [
        {
          source: "openclaw" as const,
          name: "agent",
          slug: "agent",
          command: fingerprint,
          fingerprint,
        },
      ],
    });
    detectSetupMock
      .mockResolvedValueOnce(preview("command-a"))
      .mockResolvedValueOnce(preview("command-b"));
    renderSection();
    await openAdvanced("Import ACP agents");
    fireEvent.change(screen.getByLabelText("Configuration path"), {
      target: { value: "/tmp/a.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview import/i }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /command-a/ }));
    expect(screen.getByRole("button", { name: /import selected openclaw/i })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Configuration path"), {
      target: { value: "/tmp/b.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview import/i }));
    const changed = await screen.findByRole("checkbox", { name: /command-b/ });
    expect(changed).not.toBeChecked();
    expect(screen.getByRole("button", { name: /import selected openclaw/i })).toBeDisabled();
    expect(runSetupActionMock).not.toHaveBeenCalled();
    fireEvent.click(changed);
    fireEvent.click(screen.getByRole("button", { name: /import selected openclaw/i }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "import_acp",
        source: "openclaw",
        names: ["agent"],
        path: "/tmp/b.json",
        fingerprints: { agent: "command-b" },
      }),
    );
  });

  it("preserves an ACP import path and preview fingerprints through confirmation", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    detectSetupMock.mockResolvedValue({
      providers: [],
      models: {},
      default_models: {},
      imports: [
        {
          source: "openclaw",
          name: "review-agent",
          slug: "review-agent",
          command: "review-agent (2 arguments hidden)",
          model: null,
          fingerprint: "sha256-preview",
        },
      ],
    });
    renderSection();

    await openAdvanced("Import ACP agents");
    fireEvent.change(screen.getByLabelText("Configuration path"), {
      target: { value: "/tmp/openclaw.json" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview import/i }));

    await waitFor(() =>
      expect(detectSetupMock).toHaveBeenCalledWith("mac", {
        import_source: "openclaw",
        import_path: "/tmp/openclaw.json",
      }),
    );
    fireEvent.click(await screen.findByRole("checkbox", { name: /review-agent/i }));
    fireEvent.click(screen.getByRole("button", { name: /import selected openclaw agents/i }));

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "import_acp",
        source: "openclaw",
        names: ["review-agent"],
        path: "/tmp/openclaw.json",
        fingerprints: { "review-agent": "sha256-preview" },
      }),
    );
  });

  it("accepts an optional model override and clears the key after acknowledgement", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAdvanced("Add a provider");
    fireEvent.click(screen.getByRole("button", { name: /add provider/i }));
    const save = screen.getByRole("button", { name: /save provider/i });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "More options" }));
    fireEvent.change(screen.getByLabelText("Default model"), { target: { value: "gpt-5.6" } });
    fireEvent.change(screen.getByLabelText("API key"), {
      target: { value: "super-secret" },
    });
    fireEvent.click(save);

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_key",
        provider: "openai",
        name: undefined,
        model: "gpt-5.6",
        secret: "super-secret",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /save provider/i })).toBeNull(),
    );

    fireEvent.click(screen.getByRole("button", { name: /add provider/i }));
    expect(screen.getByLabelText("API key")).toHaveValue("");
  });

  it("requires a model immediately when an explicit catalog check reports none", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    detectSetupMock.mockResolvedValue({
      providers: [],
      imports: [],
      models: {},
      default_models: { openai: null },
    });
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "Find credentials on this computer" }));
    await screen.findByText("No additional credentials were found.");
    fireEvent.click(screen.getByRole("button", { name: "API key" }));
    expect(
      screen.getByText("This vendor has no catalog default on this computer."),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "test-only-key" } });
    const save = screen.getByRole("button", { name: "Save provider" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("Required for this vendor"), {
      target: { value: "gpt-5.6" },
    });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_key",
        provider: "openai",
        name: undefined,
        model: "gpt-5.6",
        secret: "test-only-key",
      }),
    );
  });

  it("sends gateway families, models, selected protocol, and credential payload", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAdvanced("Add a provider");
    fireEvent.click(screen.getByRole("button", { name: /add gateway/i }));
    fireEvent.change(screen.getByLabelText("Gateway name"), { target: { value: "relay" } });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example" },
    });
    fireEvent.change(screen.getByLabelText("Anthropic model"), {
      target: { value: "claude-sonnet" },
    });
    fireEvent.change(screen.getByLabelText("OpenAI model"), { target: { value: "gpt-5.6" } });
    fireEvent.change(screen.getAllByTestId("mock-select").at(-1) as HTMLSelectElement, {
      target: { value: "chat" },
    });
    fireEvent.change(screen.getByLabelText("API key or token"), {
      target: { value: "gateway-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save gateway/i }));

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_gateway",
        name: "relay",
        base_url: "https://relay.example",
        families: ["anthropic", "openai"],
        wire_api: "chat",
        models: { anthropic: "claude-sonnet", openai: "gpt-5.6" },
        secret: "gateway-secret",
      }),
    );
  });

  it("sends the complete Bedrock form without exposing the saved secret afterward", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAdvanced("Add a provider");
    fireEvent.click(screen.getByRole("button", { name: /add bedrock/i }));
    fireEvent.change(screen.getByLabelText("Model ID"), {
      target: { value: "anthropic.claude-sonnet" },
    });
    fireEvent.change(screen.getByLabelText("Environment variable"), {
      target: { value: "AWS_BEARER_TOKEN_BEDROCK" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save bedrock/i }));

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_bedrock",
        name: "bedrock",
        base_url: "https://bedrock-runtime.us-east-1.amazonaws.com",
        model: "anthropic.claude-sonnet",
        env_var: "AWS_BEARER_TOKEN_BEDROCK",
      }),
    );
    expect(screen.queryByRole("button", { name: /save bedrock/i })).toBeNull();
  });

  it("offers only supported guided commands and starts them with typed parameters", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({ supported_operations: ["codex-login", "databricks-configure"] }),
    );
    startSetupOperationMock
      .mockResolvedValueOnce({
        operation_id: "op-codex",
        state: "succeeded",
        action: "codex-login",
        exit_code: 0,
        error: null,
      })
      .mockResolvedValueOnce({
        operation_id: "op-databricks",
        state: "running",
        action: "databricks-configure",
        exit_code: null,
        error: null,
      });
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await waitFor(() =>
      expect(startSetupOperationMock).toHaveBeenCalledWith("mac", "codex-login", {}),
    );
    fireEvent.click(screen.getByRole("button", { name: "Databricks" }));

    fireEvent.change(screen.getByLabelText("Workspace URL"), {
      target: { value: "https://workspace.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^configure$/i }));
    await waitFor(() =>
      expect(startSetupOperationMock).toHaveBeenCalledWith("mac", "databricks-configure", {
        workspace_url: "https://workspace.example.com",
        agents: ["codex"],
      }),
    );
  });

  it("offers Databricks setup for the current harness or all standard harnesses", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["databricks-configure"] }));
    renderSection();

    await openAgent("claude");
    fireEvent.click(screen.getByRole("button", { name: "Databricks" }));
    fireEvent.change(screen.getByLabelText("Configure for"), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText("Workspace URL"), {
      target: { value: "https://workspace.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^configure$/i }));

    await waitFor(() =>
      expect(startSetupOperationMock).toHaveBeenCalledWith("mac", "databricks-configure", {
        workspace_url: "https://workspace.example.com",
        agents: ["claude", "codex", "pi"],
      }),
    );
  });

  it("keeps a new operation and its recovery ID when an old cancellation arrives", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    renderSection();
    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await screen.findByTestId("setup-terminal");
    fireEvent.click(screen.getByRole("button", { name: "Simulate delayed cancellation" }));
    fireEvent.click(screen.getByRole("button", { name: "Simulate operation completion" }));
    startSetupOperationMock.mockResolvedValue({
      operation_id: "op-2",
      state: "running",
      action: "codex-login",
      exit_code: null,
      error: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await waitFor(() =>
      expect(screen.getByTestId("setup-terminal")).toHaveAttribute("data-operation-id", "op-2"),
    );

    act(() => deliverDelayedCancellation?.());

    expect(screen.getByTestId("setup-terminal")).toHaveAttribute("data-operation-id", "op-2");
    expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBe("op-2");
    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeDisabled();
  });

  it("does not reopen a completed operation when a delayed poll reports it as running", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    renderSection();
    await openAgent("codex");

    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await screen.findByTestId("setup-terminal");
    fireEvent.click(screen.getByRole("button", { name: "Simulate operation completion" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeEnabled(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Simulate stale running poll" }));

    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeEnabled();
    expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBeNull();
  });

  it("refreshes the selected host's sign-in commands after installing a harness", async () => {
    hosts = [{ ...online("mac"), configured_harnesses: { "codex-native": "binary-missing" } }];
    inventories.set("mac", inventory({ supported_operations: [] }));
    installHarnessMock.mockImplementation((_harness, options) => {
      inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
      options.onSuccess();
    });
    renderSection();
    await openAgent("codex");
    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeEnabled(),
    );
    expect(fetchInventoryMock.mock.calls.map(([hostId]) => hostId)).toEqual(["mac", "mac"]);
  });

  it("sends only standard setup fields when creating a custom ACP agent", async () => {
    hosts = [online("mac")];
    renderSection();
    await openAdvanced("Custom ACP agents");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Example" } });
    fireEvent.change(screen.getByLabelText("Launch command"), {
      target: { value: "example --acp" },
    });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "test-model" } });
    fireEvent.click(screen.getByRole("button", { name: "Add ACP agent" }));

    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "add_acp",
        name: "Example",
        command: "example --acp",
        model: "test-model",
      }),
    );
  });

  it("reveals a newly started operation once and locks competing setup actions", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({ supported_operations: ["codex-login", "databricks-configure"] }),
    );
    renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "Databricks" }));
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));

    await waitFor(() =>
      expect(scrollIntoViewMock).toHaveBeenCalledWith({ behavior: "smooth", block: "center" }),
    );
    expect(scrollIntoViewMock).toHaveBeenCalledOnce();
    expect(
      screen.getByRole("button", { name: /find credentials on this computer/i }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /configure$/i })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /simulate operation poll/i }));
    await waitFor(() => expect(screen.getByTestId("setup-terminal")).toBeInTheDocument());
    expect(scrollIntoViewMock).toHaveBeenCalledOnce();
  });

  it("restores an active operation after the settings page remounts", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    const first = renderSection();

    await openAgent("codex");
    fireEvent.click(screen.getByRole("button", { name: "ChatGPT subscription" }));
    await waitFor(() =>
      expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBe("op-1"),
    );
    first.unmount();
    scrollIntoViewMock.mockReset();
    let finishRecovery!: (operation: SetupOperation) => void;
    fetchSetupOperationMock.mockImplementationOnce(
      () =>
        new Promise<SetupOperation>((resolve) => {
          finishRecovery = resolve;
        }),
    );

    renderSection();

    expect(await screen.findByText("Restoring setup operation…")).toBeInTheDocument();
    await openAdvanced("Manage all connections");
    expect(screen.getByRole("button", { name: /detect credentials/i })).toBeDisabled();
    finishRecovery({
      operation_id: "op-1",
      state: "running",
      action: "codex-login",
      exit_code: null,
      error: null,
    });
    await waitFor(() =>
      expect(fetchSetupOperationMock).toHaveBeenCalledWith("mac", "op-1", expect.any(AbortSignal)),
    );
    expect(await screen.findByTestId("setup-terminal")).toBeInTheDocument();
    expect(scrollIntoViewMock).not.toHaveBeenCalled();
  });

  it("keeps recovered operations isolated to their original computer", async () => {
    hosts = [online("mac", "Mac"), online("linux", "Linux")];
    inventories.set("mac", inventory());
    inventories.set("linux", inventory());
    localStorage.setItem("omnigent:provider-settings-host", "mac");
    sessionStorage.setItem("omnigent:provider-setup-operation:mac", "op-mac");
    fetchSetupOperationMock.mockResolvedValue({
      operation_id: "op-mac",
      state: "running",
      action: "codex-login",
      exit_code: null,
      error: null,
    });
    renderSection();

    expect(await screen.findByTestId("setup-terminal")).toBeInTheDocument();
    fireEvent.change(hostSelect(), { target: { value: "linux" } });

    await waitFor(() => expect(screen.queryByTestId("setup-terminal")).toBeNull());
    expect(fetchSetupOperationMock).toHaveBeenCalledTimes(1);
    expect(fetchSetupOperationMock).toHaveBeenCalledWith("mac", "op-mac", expect.any(AbortSignal));
    expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBe("op-mac");
  });

  it("clears a stale remembered operation after host revalidation", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory({ supported_operations: ["codex-login"] }));
    sessionStorage.setItem("omnigent:provider-setup-operation:mac", "op-stale");
    fetchSetupOperationMock.mockRejectedValue(
      Object.assign(new Error("Unknown setup operation"), { status: 404 }),
    );
    renderSection();

    await waitFor(() =>
      expect(sessionStorage.getItem("omnigent:provider-setup-operation:mac")).toBeNull(),
    );
    expect(screen.queryByTestId("setup-terminal")).toBeNull();
    await openAgent("codex");
    expect(screen.getByRole("button", { name: "ChatGPT subscription" })).toBeEnabled();
    expect(screen.queryByText(/could not be restored/i)).toBeNull();
  });

  it("does not offer managed credential rotation for subscription providers", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "codex-subscription",
            kind: "subscription",
            families: ["openai"],
            defaults: ["openai"],
            default_scopes: ["openai"],
            credential_sources: {},
            models: { openai: "gpt-5.6-codex" },
            base_urls: {},
          },
        ],
      }),
    );
    renderSection();

    await openAdvanced("Manage all connections");
    const row = await screen.findByTestId("provider-row-codex-subscription");
    expect(within(row).getByText("codex-subscription")).toBeInTheDocument();
    expect(within(row).getByText("subscription")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /update credential/i })).toBeNull();
    expect(screen.getByText("Advanced provider tools")).toBeInTheDocument();
  });

  it("writes harness keys and per-harness model settings through typed actions", async () => {
    hosts = [online("mac")];
    inventories.set("mac", inventory());
    renderSection();

    await openAgent("cursor");
    fireEvent.click(screen.getByRole("button", { name: "Cursor API key" }));
    fireEvent.click(screen.getByRole("button", { name: /^add key$/i }));
    fireEvent.change(screen.getByLabelText("Environment variable"), {
      target: { value: "CURSOR_API_KEY" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save key/i }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_harness_key",
        harness: "cursor",
        env_var: "CURSOR_API_KEY",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Back to agents" }));
    await openAgent("opencode");
    fireEvent.click(screen.getByRole("button", { name: "Default model" }));
    fireEvent.change(screen.getByLabelText(/^OpenCode default model/), {
      target: { value: "openai/gpt-5.6" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save model/i }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "set_opencode_model",
        model: "openai/gpt-5.6",
      }),
    );
  });

  it("shows the host removal warning and waits for confirmation", async () => {
    hosts = [online("mac")];
    inventories.set(
      "mac",
      inventory({
        providers: [
          {
            name: "risky-gateway",
            kind: "gateway",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
            remove_warning: "Existing sessions will retain this gateway until they stop.",
          },
        ],
      }),
    );
    renderSection();

    await openAdvanced("Manage all connections");
    await screen.findByText("risky-gateway");
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    expect(screen.getByRole("alertdialog", { name: /remove risky-gateway/i })).toHaveTextContent(
      "Existing sessions will retain this gateway until they stop.",
    );
    expect(runSetupActionMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /remove provider/i }));
    await waitFor(() =>
      expect(runSetupActionMock).toHaveBeenCalledWith("mac", {
        action: "remove_provider",
        name: "risky-gateway",
      }),
    );
  });

  it("clears an open connection form when switching computers", async () => {
    hosts = [online("mac", "Mac"), online("linux", "Linux")];
    localStorage.setItem("omnigent:provider-settings-host", "mac");
    inventories.set("mac", inventory());
    inventories.set(
      "linux",
      inventory({
        providers: [
          {
            name: "linux-only",
            kind: "api_key",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
      }),
    );
    renderSection();

    await openAdvanced("Add a provider");
    fireEvent.click(screen.getByRole("button", { name: /add provider/i }));
    fireEvent.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.getByLabelText("Default model")).toBeInTheDocument();
    fireEvent.change(hostSelect(), { target: { value: "linux" } });
    await openAgent("codex");
    expect(await screen.findByText("linux-only")).toBeInTheDocument();
    expect(screen.queryByLabelText("Default model")).toBeNull();
  });

  it("does not render an old computer's response after switching", async () => {
    let resolveMac!: (value: SetupInventory) => void;
    const macInventory = new Promise<SetupInventory>((resolve) => {
      resolveMac = resolve;
    });
    hosts = [online("mac", "Mac"), online("linux", "Linux")];
    localStorage.setItem("omnigent:provider-settings-host", "mac");
    inventories.set(
      "linux",
      inventory({
        providers: [
          {
            name: "linux-only",
            kind: "api_key",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
      }),
    );
    fetchInventoryMock.mockImplementation((hostId) =>
      hostId === "mac" ? macInventory : Promise.resolve(inventories.get(hostId) ?? inventory()),
    );
    renderSection();

    await waitFor(() =>
      expect(fetchInventoryMock).toHaveBeenCalledWith("mac", expect.any(AbortSignal)),
    );
    fireEvent.change(hostSelect(), { target: { value: "linux" } });
    await openAdvanced("Manage all connections");
    expect(await screen.findByText("linux-only")).toBeInTheDocument();
    resolveMac(
      inventory({
        providers: [
          {
            name: "stale-mac-provider",
            kind: "api_key",
            families: ["openai"],
            defaults: [],
            default_scopes: ["openai"],
            credential_sources: {},
            models: {},
            base_urls: {},
          },
        ],
      }),
    );
    await waitFor(() => expect(screen.queryByText("stale-mac-provider")).toBeNull());
  });
});
