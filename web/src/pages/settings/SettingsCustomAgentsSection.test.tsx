import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsCustomAgentsSection } from "./SettingsCustomAgentsSection";
import { authenticatedFetch } from "@/lib/identity";
import { buildAgentBundle } from "@/lib/agentBundle";
import type { ManagedAgent } from "@/hooks/useCustomAgents";

const flags = vi.hoisted(() => ({ feature: true, install: true }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    features: { custom_agents_settings_ui: flags.feature },
    agent_install: flags.install,
  }),
}));
vi.mock("@/lib/agentLabels", () => ({
  BRAIN_HARNESS_LABELS: { "claude-sdk": "Claude SDK" },
  useAcpHarnessIds: () => new Set(["jcode"]),
  useHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
  useBrainHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/agentBundle", () => ({ buildAgentBundle: vi.fn() }));

const agent = (id: string, name = id): ManagedAgent => ({
  id,
  name,
  description: `${name} description`,
  harness: "claude-sdk",
  version: 1,
  created_at: 1780000000,
  updated_at: null,
  builtin: false,
  mcp_servers: [],
  skills: [],
});
const polly = {
  ...agent("builtin-polly", "Polly"),
  builtin: true,
  skills: [{ name: "review", description: "Review code" }],
};
const page = (data: ManagedAgent[], has_more = false, last_id: string | null = null) => ({
  data,
  has_more,
  last_id,
});
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let users: ManagedAgent[];
let failSave: boolean;
let failDelete: boolean;
let inUse: boolean;

function mount(path = "/settings/custom-agents") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <SettingsCustomAgentsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  flags.feature = true;
  flags.install = true;
  failSave = false;
  failDelete = false;
  inUse = false;
  users = [agent("a1", "research"), agent("a2", "research")];
  vi.mocked(buildAgentBundle).mockResolvedValue(new File(["bundle"], "agent.tar.gz"));
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (init?.method === "POST") {
      if (failSave)
        return response({ error: { message: "Invalid bundle", code: "invalid_input" } }, 400);
      const saved = agent("saved", "new-agent");
      users = [saved, ...users];
      return response(saved);
    }
    if (init?.method === "DELETE") {
      if (failDelete)
        return response({ error: { message: "Delete failed", code: "internal_error" } }, 500);
      if (inUse && !url.searchParams.has("force"))
        return response(
          { error: { code: "agent_in_use", message: "Agent in use" }, sessions_in_use: "2" },
          409,
        );
      users = users.filter((a) => a.id !== url.pathname.split("/").at(-1));
      return response({ deleted: true });
    }
    return response(
      page(
        url.searchParams.get("scope") === "user"
          ? users
          : [polly, { ...agent("jcode"), builtin: true, harness: "jcode" }],
      ),
    );
  });
});
afterEach(cleanup);

describe("Custom agents settings", () => {
  it("keeps duplicate names by ID and reviews server agents without mutation actions", async () => {
    mount();
    expect(await screen.findAllByRole("link", { name: "research" })).toHaveLength(2);
    expect(screen.queryByRole("link", { name: /jcode/ })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: /Polly description/ }));
    expect(await screen.findByRole("heading", { name: "Polly" })).toBeVisible();
    expect(screen.getByText("Review code")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete agent" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("continues through an empty page when opening a bookmarked agent", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("scope") !== "user") return response(page([polly]));
      return response(
        url.searchParams.has("after") ? page([agent("later")]) : page([], true, "skipped-copy"),
      );
    });
    mount("/settings/custom-agents/later");
    expect(await screen.findByRole("heading", { name: "later" })).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledWith(
      expect.stringContaining("after=skipped-copy"),
      expect.anything(),
    );
  });

  it("retains form input after failure and saves a bundle without creating a session", async () => {
    failSave = true;
    mount("/settings/custom-agents/new");
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: "new-agent" } });
    fireEvent.change(screen.getByLabelText(/Model/), { target: { value: "test-model" } });
    fireEvent.change(screen.getByLabelText("System instructions"), {
      target: { value: "Keep this draft." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save agent" }));
    expect(screen.getByLabelText(/Name/)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add server" })).toBeDisabled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid bundle");
    expect(screen.getByLabelText(/Name/)).toBeEnabled();
    expect(screen.getByLabelText("System instructions")).toHaveValue("Keep this draft.");
    failSave = false;
    fireEvent.click(screen.getByRole("button", { name: "Save agent" }));
    expect(await screen.findByRole("heading", { name: "new-agent" })).toBeVisible();
    expect(buildAgentBundle).toHaveBeenCalledWith(
      expect.objectContaining({ name: "new-agent", instructions: "Keep this draft." }),
    );
    const writes = vi
      .mocked(authenticatedFetch)
      .mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes.map(([url]) => url)).toEqual(["/v1/agents", "/v1/agents"]);
    expect((writes[1][1]!.body as FormData).get("bundle")).toBeInstanceOf(File);
  });

  it("requires a second explicit confirmation for an in-use agent and retains errors", async () => {
    inUse = true;
    mount("/settings/custom-agents/a1");
    fireEvent.click(await screen.findByRole("button", { name: "Delete agent" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      vi.mocked(authenticatedFetch).mock.calls.some(([, init]) => init?.method === "DELETE"),
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Delete agent" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText(/2 session\(s\) still use/)).toBeVisible();
    expect(authenticatedFetch).not.toHaveBeenCalledWith(
      expect.stringContaining("force=true"),
      expect.anything(),
    );
    failDelete = true;
    fireEvent.click(screen.getByRole("button", { name: "Remove anyway" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Delete failed");
    failDelete = false;
    fireEvent.click(screen.getByRole("button", { name: "Remove anyway" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findAllByRole("link", { name: "research" })).toHaveLength(1);
  });

  it.each([
    [false, true],
    [true, false],
  ])("does not fetch when feature=%s, install=%s", (feature, install) => {
    flags.feature = feature;
    flags.install = install;
    mount();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});
