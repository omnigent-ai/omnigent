import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as renderUi, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import type * as CapabilitiesModule from "@/lib/CapabilitiesContext";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProactiveAgentSettings } from "@/hooks/useProactiveAgentSettings";
import { PROACTIVE_AGENT_WORKSPACE_ENTRY } from "@/pages/settings/search/catalog/admin";
import { PROACTIVE_AGENT_ENTRY } from "@/pages/settings/search/catalog/general";
import { ProactiveAgentSettingControl } from "./ProactiveAgentSettingControl";

const mocks = vi.hoisted(() => ({
  available: true,
  settings: undefined as ProactiveAgentSettings | undefined,
  query: { isError: false, isFetching: false, refetch: vi.fn() },
  mutate: vi.fn(),
}));
// The Settings row shows by the same server answer the settings hook gives.
vi.mock("@/lib/CapabilitiesContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  useServerInfo: () => ({ proactive_agent_available: mocks.available }),
}));
vi.mock("@/hooks/useProactiveAgentSettings", () => ({
  useProactiveAgentSettings: () => ({
    available: mocks.available,
    settings: mocks.available ? mocks.settings : undefined,
    query: mocks.query,
  }),
  useSetProactiveAgentSetting: () => ({ mutate: mocks.mutate, isPending: false }),
}));

/** Renders inside a router and a fresh query client, as Settings rows need both. */
function render(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderUi(
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

function settings(overrides: Partial<ProactiveAgentSettings> = {}): ProactiveAgentSettings {
  return {
    workspace_enabled: true,
    user_enabled: true,
    enabled: true,
    can_manage_workspace: false,
    agent_id: "agent_advisor",
    ...overrides,
  };
}

beforeEach(() => {
  mocks.available = true;
  mocks.settings = settings();
  mocks.query = { isError: false, isFetching: false, refetch: vi.fn() };
  mocks.mutate.mockReset();
});
afterEach(cleanup);

describe("ProactiveAgentSettingControl", () => {
  it("renders nothing where the server does not offer Omni Advisor", () => {
    mocks.available = false;
    const { container } = render(
      <ProactiveAgentSettingControl scope="me" entry={PROACTIVE_AGENT_ENTRY} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says the settings are loading rather than hiding the switch", () => {
    mocks.settings = undefined;
    render(
      <ProactiveAgentSettingControl scope="workspace" entry={PROACTIVE_AGENT_WORKSPACE_ENTRY} />,
    );
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it.each(["me", "workspace"] as const)(
    "offers a retry when the initial %s settings read failed",
    (scope) => {
      mocks.settings = undefined;
      mocks.query.isError = true;
      render(
        <ProactiveAgentSettingControl
          scope={scope}
          entry={scope === "me" ? PROACTIVE_AGENT_ENTRY : PROACTIVE_AGENT_WORKSPACE_ENTRY}
        />,
      );
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load Omni Advisor settings.");
      expect(screen.queryByRole("switch")).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(mocks.query.refetch).toHaveBeenCalledOnce();
    },
  );

  it.each([
    ["loading", false],
    ["failed", true],
  ] as const)("keeps the row's other controls while its settings are %s", (_state, isError) => {
    mocks.settings = undefined;
    mocks.query.isError = isError;
    render(
      <ProactiveAgentSettingControl scope="workspace" entry={PROACTIVE_AGENT_WORKSPACE_ENTRY}>
        <button type="button">Run now</button>
      </ProactiveAgentSettingControl>,
    );
    const runNow = screen.getByRole("button", { name: "Run now" });
    // Inside the row, so a Settings link to its part still reaches it.
    expect(runNow.closest("[data-settings-item]")).toHaveAttribute(
      "data-settings-item",
      PROACTIVE_AGENT_WORKSPACE_ENTRY.id,
    );
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it.each([
    ["loading", false],
    ["failed", true],
  ] as const)("keeps an edit in the row's other controls once %s settings load", (_s, isError) => {
    mocks.settings = undefined;
    mocks.query.isError = isError;
    const control = () => (
      <ProactiveAgentSettingControl scope="workspace" entry={PROACTIVE_AGENT_WORKSPACE_ENTRY}>
        <textarea aria-label="Prompt" defaultValue="Saved prompt" />
      </ProactiveAgentSettingControl>
    );
    const view = render(control());
    const prompt = screen.getByRole("textbox", { name: "Prompt" });
    fireEvent.change(prompt, { target: { value: "Edited prompt" } });

    mocks.settings = settings({ can_manage_workspace: true });
    mocks.query.isError = false;
    view.rerender(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>{control()}</QueryClientProvider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("switch")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Prompt" })).toBe(prompt);
    expect(prompt).toHaveValue("Edited prompt");
  });

  it("turns the viewer's own setting off", () => {
    render(<ProactiveAgentSettingControl scope="me" entry={PROACTIVE_AGENT_ENTRY} />);
    const toggle = screen.getByRole("switch", { name: "Omni Advisor" });
    expect(toggle).toBeChecked();

    fireEvent.click(toggle);

    expect(mocks.mutate).toHaveBeenCalledWith(
      { scope: "me", enabled: false },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it("shows the viewer's switch off and locked when an admin turned it off", () => {
    mocks.settings = settings({ workspace_enabled: false, enabled: false });
    render(<ProactiveAgentSettingControl scope="me" entry={PROACTIVE_AGENT_ENTRY} />);

    const toggle = screen.getByRole("switch", { name: "Omni Advisor" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toBeDisabled();
    expect(
      screen.getByText("An admin turned Omni Advisor off for this workspace."),
    ).toBeInTheDocument();
  });

  it("offers the workspace switch only to callers who can manage it", () => {
    const { container } = render(
      <ProactiveAgentSettingControl scope="workspace" entry={PROACTIVE_AGENT_WORKSPACE_ENTRY} />,
    );
    expect(container).toBeEmptyDOMElement();
    cleanup();

    mocks.settings = settings({ can_manage_workspace: true });
    render(
      <ProactiveAgentSettingControl scope="workspace" entry={PROACTIVE_AGENT_WORKSPACE_ENTRY} />,
    );
    fireEvent.click(screen.getByRole("switch", { name: "Omni Advisor" }));
    expect(mocks.mutate).toHaveBeenCalledWith(
      { scope: "workspace", enabled: false },
      expect.anything(),
    );
  });
});
