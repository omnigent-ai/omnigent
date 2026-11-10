import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProactiveAgentSettings } from "@/hooks/useProactiveAgentSettings";
import { ProactiveAgentSettingControl } from "./ProactiveAgentSettingControl";

const mocks = vi.hoisted(() => ({
  settings: undefined as ProactiveAgentSettings | undefined,
  mutate: vi.fn(),
}));
vi.mock("@/hooks/useProactiveAgentSettings", () => ({
  useProactiveAgentSettings: () => ({
    available: mocks.settings !== undefined,
    settings: mocks.settings,
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
  mocks.settings = settings();
  mocks.mutate.mockReset();
});
afterEach(cleanup);

describe("ProactiveAgentSettingControl", () => {
  it("renders nothing where the server does not offer Omni Advisor", () => {
    mocks.settings = undefined;
    const { container } = render(<ProactiveAgentSettingControl scope="me" />);
    expect(container).toBeEmptyDOMElement();
  });

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
