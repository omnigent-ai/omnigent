import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host, ProviderConnectionState, ProviderInventoryEntry } from "@/hooks/useHosts";

const mocks = vi.hoisted(() => ({
  providers: [] as ProviderInventoryEntry[],
  isLoading: false,
  isError: false,
  inventoryArgs: [] as [string | null | undefined, boolean][],
}));

vi.mock("@/hooks/useHosts", () => ({
  useProviderInventory: (hostId: string | null | undefined, enabled: boolean) => {
    mocks.inventoryArgs.push([hostId, enabled]);
    return { data: mocks.providers, isLoading: mocks.isLoading, isError: mocks.isError };
  },
}));

const { NewChatProviderStatus, providerForHarness } = await import("./NewChatProviderStatus");

function providerRow(partial: Partial<ProviderInventoryEntry> = {}): ProviderInventoryEntry {
  return {
    id: "codex",
    display_name: "Codex",
    kind: "subscription",
    origin: "configured",
    source: "config",
    configuration_state: "valid",
    error: null,
    families: ["openai"],
    surfaces: ["openai"],
    default_for: ["openai"],
    default_models: {},
    cli: "codex",
    profile: null,
    model_provider: null,
    capabilities: {
      model_discovery: "supported",
      usage_status: "unsupported",
      multiple_profiles: "unsupported",
      interactive_cli: "supported",
    },
    default_for_harnesses: ["codex-native"],
    connection_state: "connected",
    connection_detail:
      "A credential reference is configured locally; the provider was not contacted.",
    ...partial,
  };
}

const HOST_ONE = { host_id: "host_1", name: "MacBook" } as Host;
const HOST_TWO = { host_id: "host_2", name: "Workstation" } as Host;

beforeEach(() => {
  mocks.providers = [];
  mocks.isLoading = false;
  mocks.isError = false;
  mocks.inventoryArgs = [];
});

afterEach(cleanup);

describe("providerForHarness", () => {
  it("uses the host's canonical mapping rather than guessing from provider families", () => {
    const rows = [
      providerRow({ id: "work", display_name: "Work", default_for_harnesses: ["pi-native"] }),
      providerRow(),
    ];

    expect(providerForHarness(rows, "codex-native")?.id).toBe("codex");
    expect(providerForHarness(rows, "pi-native")?.id).toBe("work");
    expect(providerForHarness(rows, "claude-native")).toBeNull();
  });

  it("does not invent an answer for an older host or an absent harness", () => {
    const legacy = providerRow();
    delete legacy.default_for_harnesses;

    expect(providerForHarness([legacy], "codex-native")).toBeNull();
    expect(providerForHarness(undefined, "codex-native")).toBeNull();
    expect(providerForHarness([providerRow()], null)).toBeNull();
  });
});

describe("NewChatProviderStatus", () => {
  it("shows the serving provider with conservative local evidence", () => {
    mocks.providers = [providerRow()];
    render(<NewChatProviderStatus host={HOST_ONE} harness="codex-native" open />);

    const status = screen.getByTestId("new-chat-provider-status");
    expect(status).toHaveAttribute("data-state", "connected");
    expect(status).toHaveTextContent("Codex");
    expect(status).toHaveTextContent("Credential available locally");
    expect(status).toHaveTextContent("the provider was not contacted");
    expect(screen.getByText("Provider")).toBeInTheDocument();
  });

  it.each<[ProviderConnectionState, string]>([
    ["authentication_required", "Authentication required"],
    ["misconfigured", "Misconfigured"],
    ["unavailable", "Unavailable on this host"],
    ["unknown", "Status unknown"],
  ])("presents %s without claiming vendor connectivity", (connectionState, label) => {
    mocks.providers = [
      providerRow({
        connection_state: connectionState,
        connection_detail: "This conclusion uses local evidence only.",
      }),
    ];

    render(<NewChatProviderStatus host={HOST_ONE} harness="codex-native" open />);

    expect(screen.getByTestId("new-chat-provider-status")).toHaveAttribute(
      "data-state",
      connectionState,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText("This conclusion uses local evidence only.")).toBeInTheDocument();
  });

  it.each([
    ["missing mapping", HOST_ONE, "claude-native", false, false],
    ["sandbox", null, "codex-native", false, false],
    ["missing harness", HOST_ONE, null, false, false],
    ["closed dialog", HOST_ONE, "codex-native", false, false],
    ["loading", HOST_ONE, "codex-native", true, false],
    ["error", HOST_ONE, "codex-native", false, true],
  ])("renders no outer row while %s", (_case, host, harness, loading, error) => {
    mocks.providers = [providerRow()];
    mocks.isLoading = loading;
    mocks.isError = error;
    const open = _case !== "closed dialog";

    const { container } = render(
      <NewChatProviderStatus host={host as Host | null} harness={harness} open={open} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Provider")).toBeNull();
  });

  it("performs only the noninteractive inventory read when the dialog opens", () => {
    mocks.providers = [providerRow()];

    render(<NewChatProviderStatus host={HOST_ONE} harness="codex-native" open />);

    expect(mocks.inventoryArgs.at(-1)).toEqual(["host_1", true]);
  });

  it("clears the old row while a newly selected host loads", () => {
    mocks.providers = [providerRow()];
    const { rerender } = render(
      <NewChatProviderStatus host={HOST_ONE} harness="codex-native" open />,
    );
    expect(screen.getByText("Codex")).toBeInTheDocument();

    mocks.isLoading = true;
    rerender(<NewChatProviderStatus host={HOST_TWO} harness="codex-native" open />);
    expect(screen.queryByText("Provider")).toBeNull();
    expect(mocks.inventoryArgs.at(-1)).toEqual(["host_2", true]);

    mocks.isLoading = false;
    mocks.providers = [providerRow({ id: "work", display_name: "Work Gateway" })];
    rerender(<NewChatProviderStatus host={HOST_TWO} harness="codex-native" open />);
    expect(screen.getByText("Work Gateway")).toBeInTheDocument();
  });
});
