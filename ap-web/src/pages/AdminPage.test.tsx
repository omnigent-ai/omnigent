// Tests for the GTM control-plane Admin page (AdminPage).
//
// Mocks lib/controlPlaneApi so each test drives a specific role / response
// and asserts gating + the new affordances (publish refresh, delete, test
// connection, keyboard-operable usage drill-down).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AgentTestResult,
  ControlPlaneMe,
  ManagedAgent,
  UsageReport,
} from "@/lib/controlPlaneApi";

const api = vi.hoisted(() => ({
  getControlPlaneMe: vi.fn(),
  listControlPlaneAgents: vi.fn(),
  setAgentVisibility: vi.fn(),
  listPublishable: vi.fn(),
  publishAgent: vi.fn(),
  getUsage: vi.fn(),
  getAudit: vi.fn(),
  deleteControlPlaneAgent: vi.fn(),
  testAgent: vi.fn(),
}));

vi.mock("@/lib/controlPlaneApi", () => api);

import { AdminPage } from "./AdminPage";

const ADMIN_ME: ControlPlaneMe = {
  user_id: "admin@db.com",
  role: "admin",
  groups: [],
  is_platform_admin: false,
  capabilities: {
    can_publish: true,
    can_manage_visibility: true,
    can_view_usage: true,
    can_manage_all: true,
  },
};

const CONSUMER_ME: ControlPlaneMe = {
  user_id: "dave@db.com",
  role: "consumer",
  groups: [],
  is_platform_admin: false,
  capabilities: {
    can_publish: false,
    can_manage_visibility: false,
    can_view_usage: false,
    can_manage_all: false,
  },
};

function agent(over: Partial<ManagedAgent> = {}): ManagedAgent {
  return {
    id: "ag_1",
    name: "polly",
    description: "an agent",
    visibility: "org",
    audience: { users: [], groups: [] },
    owner_id: "admin@db.com",
    created_at: 0,
    viewer_can_manage: true,
    ...over,
  };
}

const EMPTY_USAGE: UsageReport = {
  data: [],
  totals: { total_cost_usd: 0, total_tokens: 0, session_count: 0 },
};

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  // Sensible defaults; individual tests override.
  api.listControlPlaneAgents.mockResolvedValue({ ok: true, agents: [] });
  api.listPublishable.mockResolvedValue({ ok: true, publishable: [] });
  api.getUsage.mockResolvedValue({ ok: true, report: EMPTY_USAGE });
  api.getAudit.mockResolvedValue({ ok: true, entries: [] });
});
afterEach(cleanup);

describe("AdminPage gating", () => {
  it("renders 'not available' when the control plane is absent (404)", async () => {
    api.getControlPlaneMe.mockResolvedValue({ ok: false, error: "Not found.", status: 404 });
    render(<AdminPage />);
    expect(await screen.findByText(/isn't available in this deployment/)).toBeTruthy();
    expect(api.listControlPlaneAgents).not.toHaveBeenCalled();
  });

  it("consumer sees only the role section, no Publish/Agents/Usage", async () => {
    api.getControlPlaneMe.mockResolvedValue({ ok: true, me: CONSUMER_ME });
    render(<AdminPage />);
    await screen.findByText(/Admin/);
    expect(screen.queryByText("Publish agent")).toBeNull();
    expect(screen.queryByText("Agent visibility")).toBeNull();
    expect(api.listControlPlaneAgents).not.toHaveBeenCalled();
  });

  it("admin sees Agents, Publish, Usage, and Audit", async () => {
    api.getControlPlaneMe.mockResolvedValue({ ok: true, me: ADMIN_ME });
    render(<AdminPage />);
    await screen.findByText("Agent visibility");
    // "Publish agent" appears as both a section header and a button.
    expect(screen.getAllByText("Publish agent").length).toBeGreaterThan(0);
    expect(screen.getByText("Usage")).toBeTruthy();
  });
});

describe("AdminPage agent actions", () => {
  beforeEach(() => {
    api.getControlPlaneMe.mockResolvedValue({ ok: true, me: ADMIN_ME });
  });

  it("delete confirms then removes and refreshes the list", async () => {
    api.listControlPlaneAgents
      .mockResolvedValueOnce({ ok: true, agents: [agent()] }) // initial
      .mockResolvedValueOnce({ ok: true, agents: [] }); // after delete
    api.deleteControlPlaneAgent.mockResolvedValue({ ok: true, deleted: true });
    render(<AdminPage />);
    await screen.findByText("polly");

    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    // Confirm dialog → click the destructive Delete.
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(api.deleteControlPlaneAgent).toHaveBeenCalledWith("ag_1"));
    await waitFor(() => expect(api.listControlPlaneAgents).toHaveBeenCalledTimes(2));
  });

  it("test connection shows per-check results", async () => {
    api.listControlPlaneAgents.mockResolvedValue({ ok: true, agents: [agent()] });
    const result: AgentTestResult = {
      ok: true,
      agent_id: "ag_1",
      harness: "claude-sdk",
      model: "claude",
      mcp_server_count: 0,
      checks: [
        { name: "agent_record", ok: true, detail: "id=ag_1 v1" },
        { name: "bundle_present", ok: true, detail: "ag_1/abc" },
        { name: "bundle_loadable", ok: true, detail: "harness=claude-sdk" },
        { name: "spec_valid", ok: true, detail: "valid" },
      ],
    };
    api.testAgent.mockResolvedValue({ ok: true, result });
    render(<AdminPage />);
    await screen.findByText("polly");

    fireEvent.click(screen.getByRole("button", { name: /Test/ }));
    await waitFor(() => expect(api.testAgent).toHaveBeenCalledWith("ag_1"));
    expect((await screen.findAllByText(/reachable and launchable/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText("spec_valid").length).toBeGreaterThan(0);
  });
});

describe("AdminPage publish refresh", () => {
  it("re-fetches the agent list after a successful publish", async () => {
    api.getControlPlaneMe.mockResolvedValue({ ok: true, me: ADMIN_ME });
    api.listPublishable.mockResolvedValue({
      ok: true,
      publishable: [{ session_id: "s1", agent_id: "ag_src", name: "src", title: "Src" }],
    });
    api.publishAgent.mockResolvedValue({
      ok: true,
      published: { agent_id: "ag_new", name: "new-agent", owner_id: "admin@db.com", visibility: "org" },
    });
    render(<AdminPage />);
    await screen.findByText("Agent visibility");
    const initialCalls = api.listControlPlaneAgents.mock.calls.length;

    // Open publish dialog, fill, submit.
    fireEvent.click(screen.getByRole("button", { name: /Publish agent/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("combobox"), { target: { value: "s1" } });
    fireEvent.change(within(dialog).getByPlaceholderText("deal-helper"), {
      target: { value: "new-agent" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));

    await screen.findByText(/Published/);
    // Publish invalidated the page → agents list re-fetched.
    await waitFor(() =>
      expect(api.listControlPlaneAgents.mock.calls.length).toBeGreaterThan(initialCalls),
    );
  });
});

describe("AdminPage usage drill-down", () => {
  it("is keyboard-operable (role=button, aria-expanded, Enter toggles)", async () => {
    api.getControlPlaneMe.mockResolvedValue({ ok: true, me: ADMIN_ME });
    api.getUsage.mockResolvedValue({
      ok: true,
      report: {
        data: [
          {
            agent_id: "ag_1",
            agent_name: "polly",
            total_cost_usd: 1,
            total_tokens: 100,
            session_count: 1,
            by_user: [{ user_id: "u@db.com", cost_usd: 1, total_tokens: 100, session_count: 1 }],
          },
        ],
        totals: { total_cost_usd: 1, total_tokens: 100, session_count: 1 },
      },
    });
    render(<AdminPage />);
    const row = await screen.findByRole("button", {
      name: /Toggle per-user breakdown for polly/,
    });
    expect(row.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(row, { key: "Enter" });
    await waitFor(() => expect(row.getAttribute("aria-expanded")).toBe("true"));
    expect(screen.getByText("u@db.com")).toBeTruthy();
  });
});
