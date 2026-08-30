// Tests for the Providers settings section — the read-only provider inventory
// (per host) that reuses Omni Setup's detection. Covers row rendering with
// honest status, capability chips, the host picker, error/empty/loading
// states, and the Manage affordance that reuses the harness setup dialog.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type { Host, ProviderInventoryEntry } from "@/hooks/useHosts";

const mocks = vi.hoisted(() => ({
  hostsLoading: false,
  hosts: [] as Host[],
  inventoryHostId: undefined as string | null | undefined,
  inventoryEnabled: undefined as boolean | undefined,
  inventoryLoading: false,
  inventoryError: false,
  inventoryErrorObj: null as Error | null,
  providers: [] as ProviderInventoryEntry[],
  refetch: vi.fn(),
  setupSteps: {} as Record<string, unknown[]>,
  lastSetupProps: null as { harness: string | null; host: Host | null | undefined } | null,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: false,
    login_url: null,
    single_user: false,
  }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: mocks.hosts, isLoading: mocks.hostsLoading }),
  useProviderInventory: (hostId: string | null | undefined, enabled: boolean) => {
    mocks.inventoryHostId = hostId;
    mocks.inventoryEnabled = enabled;
    return {
      data: mocks.providers,
      isLoading: mocks.inventoryLoading,
      isError: mocks.inventoryError,
      error: mocks.inventoryErrorObj,
      isFetching: false,
      refetch: mocks.refetch,
    };
  },
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => {
  const original = await importOriginal<typeof AgentLabelsModule>();
  return {
    ...original,
    useHarnessSetupSteps: () => mocks.setupSteps,
  };
});
vi.mock("@/shell/HarnessSetupDialog", () => ({
  HarnessSetupDialog: (props: {
    open: boolean;
    harness: string | null;
    host: Host | null | undefined;
  }) => {
    if (props.open) mocks.lastSetupProps = { harness: props.harness, host: props.host };
    return props.open ? (
      <div data-testid="harness-setup-stub">{`${props.harness}@${props.host?.host_id ?? ""}`}</div>
    ) : null;
  },
}));
// Radix Select portals + pointer events can't be driven in jsdom; stub to a
// native <select> (lifts the trigger's data-testid), same as SettingsPage.test.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => children;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((c) => isValidElement(c) && c.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && trigger.props && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
      >
        {kids.filter((c) => !(isValidElement(c) && c.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => children,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});

import { SettingsPage } from "./SettingsPage";

function host(id: string, name: string): Host {
  return {
    host_id: id,
    name,
    owner: "jakob",
    status: "online",
  };
}

function provider(partial: Partial<ProviderInventoryEntry>): ProviderInventoryEntry {
  return {
    id: "prov",
    display_name: "Prov",
    kind: "subscription",
    origin: "configured",
    source: "config",
    configuration_state: "valid",
    error: null,
    families: [],
    surfaces: [],
    default_for: [],
    default_models: {},
    cli: null,
    profile: null,
    model_provider: null,
    capabilities: {
      model_discovery: "supported",
      usage_status: "unsupported",
      multiple_profiles: "unknown",
      interactive_cli: "supported",
    },
    connection_state: "connected",
    connection_detail: "A usable credential is configured locally; the vendor was not contacted.",
    ...partial,
  };
}

function renderPage(path = "/settings/providers") {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mocks.hostsLoading = false;
  mocks.hosts = [host("host_1", "MacBook")];
  mocks.inventoryLoading = false;
  mocks.inventoryError = false;
  mocks.inventoryErrorObj = null;
  mocks.providers = [];
  mocks.refetch.mockReset();
  mocks.setupSteps = {};
  mocks.lastSetupProps = null;
});
afterEach(cleanup);

describe("ProvidersSection", () => {
  it("renders provider rows with honest status and metadata", () => {
    mocks.providers = [
      provider({
        id: "codex",
        display_name: "Codex",
        cli: "codex",
        families: ["openai"],
        default_models: { openai: "gpt-5.4" },
      }),
      provider({
        id: "broken",
        display_name: "Broken Gateway",
        kind: "gateway",
        origin: "detected",
        configuration_state: "invalid",
        error: "Provider configuration is invalid. Reconfigure this provider.",
        connection_state: "misconfigured",
        connection_detail: "This provider's configuration could not be parsed on this host.",
        capabilities: {
          model_discovery: "unknown",
          usage_status: "unknown",
          multiple_profiles: "unknown",
          interactive_cli: "unsupported",
        },
      }),
    ];
    renderPage();

    const codex = screen.getByTestId("provider-row-codex");
    expect(within(codex).getByText("Codex")).toBeInTheDocument();
    expect(within(codex).getByText("Credential available locally")).toBeInTheDocument();
    expect(within(codex).getByText(/vendor was not contacted/)).toBeInTheDocument();
    expect(within(codex).getByText(/Families: openai \(gpt-5\.4\)/)).toBeInTheDocument();
    expect(within(codex).getByText("CLI: codex")).toBeInTheDocument();
    expect(within(codex).queryByText("Detected")).toBeNull();

    const broken = screen.getByTestId("provider-row-broken");
    expect(within(broken).getByText("Configuration invalid")).toBeInTheDocument();
    expect(within(broken).getByText("Misconfigured")).toBeInTheDocument();
    expect(
      within(broken).getByText("Provider configuration is invalid. Reconfigure this provider."),
    ).toBeInTheDocument();
    expect(within(broken).getByText("Detected")).toBeInTheDocument();
  });

  it("renders capability chips with per-state indication", () => {
    mocks.providers = [provider({ id: "codex", display_name: "Codex" })];
    renderPage();

    expect(screen.getByTestId("provider-capability-model-discovery")).toHaveTextContent(
      "Model discovery",
    );
    expect(screen.queryByTestId("provider-capability-usage-status")).toBeNull();
    expect(screen.getByTestId("provider-capability-multiple-profiles")).toHaveTextContent(
      "Multiple profiles (?)",
    );
    expect(screen.getByTestId("provider-capability-interactive-cli")).toHaveTextContent(
      "Interactive CLI",
    );
  });

  it("distinguishes every conservative connection state without claiming a vendor check", () => {
    mocks.providers = [
      provider({ id: "local", connection_state: "connected" }),
      provider({
        id: "auth",
        connection_state: "authentication_required",
        connection_detail: "The CLI has no locally detected credential.",
      }),
      provider({
        id: "bad",
        connection_state: "misconfigured",
        connection_detail: "The endpoint variable is not set.",
      }),
      provider({
        id: "missing",
        connection_state: "unavailable",
        connection_detail: "The CLI is not installed on this host.",
      }),
      provider({
        id: "opaque",
        connection_state: "unknown",
        connection_detail: "The protected credential store was not opened.",
      }),
    ];
    renderPage();

    expect(screen.getByTestId("provider-connection-local")).toHaveTextContent(
      "Credential available locally",
    );
    expect(screen.getByTestId("provider-connection-auth")).toHaveTextContent(
      "Authentication required",
    );
    expect(screen.getByTestId("provider-connection-bad")).toHaveTextContent("Misconfigured");
    expect(screen.getByTestId("provider-connection-missing")).toHaveTextContent(
      "Unavailable on this host",
    );
    expect(screen.getByTestId("provider-connection-opaque")).toHaveTextContent("Status unknown");
    expect(screen.queryByText(/^Connected$/)).toBeNull();
  });

  it("shows the empty state when the host reports no providers", () => {
    renderPage();
    expect(screen.getByTestId("provider-empty")).toHaveTextContent(/No providers/);
  });

  it("shows a retryable error instead of an indefinite spinner", () => {
    mocks.inventoryError = true;
    mocks.inventoryErrorObj = new Error("host unreachable");
    renderPage();

    expect(screen.getByTestId("provider-error")).toHaveTextContent("host unreachable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(mocks.refetch).toHaveBeenCalledTimes(1);
  });

  it("offers Manage only for CLI providers the setup catalog knows", () => {
    mocks.setupSteps = { codex: [] };
    mocks.providers = [
      provider({ id: "codex", display_name: "Codex", cli: "codex" }),
      provider({ id: "gateway", display_name: "Work Gateway", kind: "gateway" }),
      provider({ id: "mystery", display_name: "Mystery CLI", cli: "unknown-cli" }),
    ];
    renderPage();

    expect(screen.getByTestId("provider-manage-codex")).toBeInTheDocument();
    expect(screen.queryByTestId("provider-manage-gateway")).toBeNull();
    expect(screen.queryByTestId("provider-manage-mystery")).toBeNull();

    fireEvent.click(screen.getByTestId("provider-manage-codex"));
    expect(screen.getByTestId("harness-setup-stub")).toHaveTextContent("codex@host_1");
  });

  it("switches hosts through the picker when several are connected", () => {
    mocks.hosts = [host("host_1", "MacBook"), host("host_2", "Studio")];
    renderPage();

    // Single-host label is replaced by the picker; inventory targets host_1.
    expect(screen.queryByText("Host: MacBook")).toBeNull();
    expect(mocks.inventoryHostId).toBe("host_1");

    const picker = screen.getByTestId("provider-host-select") as HTMLSelectElement;
    expect(picker.value).toBe("host_1");
    fireEvent.change(picker, { target: { value: "host_2" } });
    expect(mocks.inventoryHostId).toBe("host_2");
  });

  it("closes a provider management dialog when switching hosts", () => {
    mocks.hosts = [host("host_1", "MacBook"), host("host_2", "Studio")];
    mocks.setupSteps = { codex: [] };
    mocks.providers = [provider({ id: "codex", cli: "codex" })];
    renderPage();

    fireEvent.click(screen.getByTestId("provider-manage-codex"));
    expect(screen.getByTestId("harness-setup-stub")).toHaveTextContent("codex@host_1");

    fireEvent.change(screen.getByTestId("provider-host-select"), {
      target: { value: "host_2" },
    });
    expect(screen.queryByTestId("harness-setup-stub")).toBeNull();
  });

  it("explains what to do when no host is connected", () => {
    mocks.hosts = [];
    renderPage();
    expect(screen.getByText(/No host is connected yet/)).toBeInTheDocument();
  });

  it("shows loading states for hosts and inventory", () => {
    mocks.hostsLoading = true;
    const { unmount } = renderPage();
    expect(screen.getByText("Loading hosts…")).toBeInTheDocument();

    unmount();
    cleanup();
    mocks.hostsLoading = false;
    mocks.inventoryLoading = true;
    renderPage();
    expect(screen.getByTestId("provider-loading")).toBeInTheDocument();
  });
});
