import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProactiveAgents, useSetProactiveAgents } from "@/hooks/useProactiveAgents";
import { useSharing, useSetSharing } from "@/hooks/useSharing";
import { AdminGeneralPage } from "./AdminGeneralPage";

const mocks = vi.hoisted(() => ({
  isAdmin: true,
  singleUser: false,
  nestedAgents: true,
  proactive: false,
}));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.isAdmin }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    single_user: mocks.singleUser,
    sibling_sessions: mocks.nestedAgents,
    features: { proactive_agent: mocks.proactive },
  }),
}));
vi.mock("@/hooks/useSharing", () => ({ useSharing: vi.fn(), useSetSharing: vi.fn() }));
vi.mock("@/hooks/useProactiveAgents", () => ({
  useProactiveAgents: vi.fn(),
  useSetProactiveAgents: vi.fn(),
}));
const mutate = vi.fn();

function renderPage() {
  return render(
    <MemoryRouter>
      <AdminGeneralPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.isAdmin = true;
  mocks.singleUser = false;
  mocks.nestedAgents = true;
  mocks.proactive = false;
  vi.mocked(useSharing).mockReturnValue({
    data: {
      sibling_session_destinations: "any_host",
      sibling_session_destinations_editable: true,
      sibling_session_destinations_options: [
        "current_host",
        "current_host_or_new_sandbox",
        "any_host",
      ],
    },
  } as never);
  vi.mocked(useSetSharing).mockReturnValue({ mutate, isPending: false } as never);
  vi.mocked(useProactiveAgents).mockReturnValue({
    data: {
      enabled: false,
      editable: true,
      agents: [{ name: "proactive", label: "Inbox suggestions", description: "d", enabled: true }],
    },
  } as never);
  vi.mocked(useSetProactiveAgents).mockReturnValue({ mutate, isPending: false } as never);
});
afterEach(cleanup);

describe("AdminGeneralPage", () => {
  it.each(["disabled", "non-admin"])("does not expose destinations when %s", (mode) => {
    mocks.nestedAgents = mode !== "disabled";
    mocks.isAdmin = mode !== "non-admin";
    renderPage();
    expect(screen.queryByRole("combobox", { name: "Nested agent destinations" })).toBeNull();
    expect(useSharing).toHaveBeenLastCalledWith({ enabled: false });
  });

  it("permits the local single-user administrator", () => {
    mocks.isAdmin = false;
    mocks.singleUser = true;
    renderPage();
    expect(screen.getByRole("combobox", { name: "Nested agent destinations" })).toBeEnabled();
    expect(useSharing).toHaveBeenLastCalledWith({ enabled: true });
  });

  it("saves the selected destinations", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Admin settings" })).toBeInTheDocument();
    const trigger = screen.getByRole("combobox", { name: "Nested agent destinations" });
    expect(trigger).toHaveTextContent("Any available host");
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("option", { name: /Current host or new sandbox/ }));
    expect(mutate).toHaveBeenCalledWith(
      { sibling_session_destinations: "current_host_or_new_sandbox" },
      expect.anything(),
    );
  });

  it("disables deployment-managed destinations", () => {
    const state = vi.mocked(useSharing)().data!;
    vi.mocked(useSharing).mockReturnValue({
      data: { ...state, sibling_session_destinations_editable: false },
    } as never);
    renderPage();
    expect(screen.getByRole("combobox", { name: "Nested agent destinations" })).toBeDisabled();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("shows a settings-load failure", () => {
    vi.mocked(useSharing).mockReturnValue({ error: new Error("Load failed") } as never);
    renderPage();
    expect(screen.getByRole("alert")).toHaveTextContent("Load failed");
  });

  it("offers only advertised choices and displays save errors", async () => {
    const state = vi.mocked(useSharing)().data!;
    vi.mocked(useSharing).mockReturnValue({
      data: { ...state, sibling_session_destinations_options: ["any_host", "current_host"] },
    } as never);
    mutate.mockImplementationOnce((_value, { onError }) => onError(new Error("Save failed")));
    renderPage();
    fireEvent.click(screen.getByRole("combobox", { name: "Nested agent destinations" }));
    expect(screen.queryByRole("option", { name: /Current host or new sandbox/ })).toBeNull();
    fireEvent.click(await screen.findByRole("option", { name: /Current host only/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("Save failed");
  });

  it("turns the proactive agent on without the nesting feature", () => {
    mocks.nestedAgents = false;
    mocks.proactive = true;
    renderPage();
    expect(screen.queryByRole("combobox", { name: "Nested agent destinations" })).toBeNull();
    const toggle = screen.getByRole("switch", { name: "Inbox suggestions" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(mutate).toHaveBeenCalledWith(
      { enabled: true, agents: { proactive: true } },
      expect.anything(),
    );
  });

  it("hides the proactive section without the feature", () => {
    renderPage();
    expect(screen.queryByRole("switch", { name: "Inbox suggestions" })).toBeNull();
    expect(useProactiveAgents).not.toHaveBeenCalled();
  });
});
