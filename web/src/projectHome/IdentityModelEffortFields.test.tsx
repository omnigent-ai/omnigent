import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IdentityModelEffortFields } from "./IdentityModelEffortFields";

const mocks = vi.hoisted(() => ({
  hosts: vi.fn(),
  launch: vi.fn(),
}));

vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: mocks.hosts() }) }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => "loading" }));
vi.mock("@/hooks/useLaunchModelOptions", () => ({
  useLaunchModelOptions: (args: { enabled: boolean }) => mocks.launch(args),
}));

const EFFORTS = [{ value: "high", label: "High" }];

function options(overrides: object = {}) {
  return {
    models: [{ id: "model-a", displayName: "Model A" }],
    effortsForModel: () => EFFORTS,
    loading: false,
    error: null,
    unsupported: false,
    ...overrides,
  };
}

function renderFields(model = "", effort = "") {
  render(
    <IdentityModelEffortFields
      config={{ host_id: "h1", harness: "claude-native" }}
      agentId=""
      model={model}
      effort={effort}
      disabled={false}
      onChange={() => {}}
    />,
  );
}

describe("IdentityModelEffortFields", () => {
  beforeEach(() => {
    mocks.hosts.mockReset();
    mocks.launch.mockReset().mockImplementation(() => options());
  });

  it("offers the host's models with a project default", () => {
    mocks.hosts.mockReturnValue([{ host_id: "h1", name: "laptop", status: "online" }]);
    renderFields("model-a", "high");
    expect(screen.getByTestId("project-team-model")).toHaveTextContent("Model A");
    expect(screen.getByTestId("project-team-model")).toBeEnabled();
    expect(screen.getByTestId("project-team-effort")).toHaveTextContent("High");
    expect(screen.queryByTestId("project-team-model-hint")).toBeNull();
  });

  it("disables both dropdowns with a hint when the host is offline", () => {
    mocks.hosts.mockReturnValue([{ host_id: "h1", name: "laptop", status: "offline" }]);
    mocks.launch.mockImplementation((args: { enabled: boolean }) =>
      options({ models: args.enabled ? options().models : [] }),
    );
    renderFields("model-a");
    expect(screen.getByTestId("project-team-model")).toBeDisabled();
    expect(screen.getByTestId("project-team-model")).toHaveTextContent("model-a");
    expect(screen.getByTestId("project-team-effort")).toBeDisabled();
    expect(screen.getByTestId("project-team-model-hint")).toHaveTextContent(
      "laptop is offline. Reconnect it to choose a model.",
    );
  });

  it("shows the project default when nothing is chosen", () => {
    mocks.hosts.mockReturnValue([{ host_id: "h1", name: "laptop", status: "online" }]);
    renderFields();
    expect(screen.getByTestId("project-team-agent")).toHaveTextContent("Project default");
    expect(screen.getByTestId("project-team-model")).toHaveTextContent("Project default");
    expect(screen.getByTestId("project-team-effort")).toHaveTextContent("Project default");
  });

  it("keeps a saved agent the catalog doesn't list", () => {
    mocks.hosts.mockReturnValue([{ host_id: "h1", name: "laptop", status: "online" }]);
    render(
      <IdentityModelEffortFields
        config={{ host_id: "h1" }}
        agentId="ag_saved"
        model=""
        effort=""
        disabled={false}
        defaultLabel="Default"
        onChange={() => {}}
      />,
    );
    expect(screen.getByTestId("project-team-agent")).toHaveTextContent("ag_saved");
    expect(screen.getByTestId("project-team-model")).toHaveTextContent("Default");
  });
});
