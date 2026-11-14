import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProactiveAgentSettings } from "@/hooks/useProactiveAgentSettings";
import { ProactiveAgentSettingControl } from "./ProactiveAgentSettingControl";

const mocks = vi.hoisted(() => ({
  available: true,
  settings: undefined as ProactiveAgentSettings | undefined,
  query: { isError: false, isFetching: false, refetch: vi.fn() },
  mutate: vi.fn(),
}));
vi.mock("@/hooks/useProactiveAgentSettings", () => ({
  useProactiveAgentSettings: () => ({
    available: mocks.available,
    settings: mocks.available ? mocks.settings : undefined,
    query: mocks.query,
  }),
  useSetProactiveAgentSetting: () => ({ mutate: mocks.mutate, isPending: false }),
}));

function settings(overrides: Partial<ProactiveAgentSettings> = {}): ProactiveAgentSettings {
  return {
    workspace_enabled: true,
    user_enabled: true,
    enabled: true,
    can_manage_workspace: false,
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
    const { container } = render(<ProactiveAgentSettingControl scope="me" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the settings are loading rather than hiding the switch", () => {
    mocks.settings = undefined;
    render(<ProactiveAgentSettingControl scope="workspace" />);
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it.each(["me", "workspace"] as const)(
    "offers a retry when the initial %s settings read failed",
    (scope) => {
      mocks.settings = undefined;
      mocks.query.isError = true;
      render(<ProactiveAgentSettingControl scope={scope} />);
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load Omni Advisor settings.");
      expect(screen.queryByRole("switch")).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(mocks.query.refetch).toHaveBeenCalledOnce();
    },
  );

  it("turns the viewer's own setting off", () => {
    render(<ProactiveAgentSettingControl scope="me" />);
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
    render(<ProactiveAgentSettingControl scope="me" />);

    const toggle = screen.getByRole("switch", { name: "Omni Advisor" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toBeDisabled();
    expect(
      screen.getByText("An admin turned Omni Advisor off for this workspace."),
    ).toBeInTheDocument();
  });

  it("offers the workspace switch only to callers who can manage it", () => {
    const { container } = render(<ProactiveAgentSettingControl scope="workspace" />);
    expect(container).toBeEmptyDOMElement();
    cleanup();

    mocks.settings = settings({ can_manage_workspace: true });
    render(<ProactiveAgentSettingControl scope="workspace" />);
    fireEvent.click(screen.getByRole("switch", { name: "Omni Advisor" }));
    expect(mocks.mutate).toHaveBeenCalledWith(
      { scope: "workspace", enabled: false },
      expect.anything(),
    );
  });
});
