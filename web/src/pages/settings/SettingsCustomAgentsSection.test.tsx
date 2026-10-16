import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsCustomAgentsSection } from "./SettingsCustomAgentsSection";
import { authenticatedFetch } from "@/lib/identity";
import { buildAgentBundle } from "@/lib/agentBundle";
import type { ManagedAgent } from "@/hooks/useCustomAgents";

const flags = vi.hoisted(() => ({
  feature: true,
  install: true,
  detail: true as boolean | undefined,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    features: { custom_agents_settings_ui: flags.feature },
    agent_install: flags.install,
    agent_detail: flags.detail,
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
  flags.detail = true;
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
    if (url.pathname.startsWith("/v1/agents/")) {
      const id = decodeURIComponent(url.pathname.slice("/v1/agents/".length));
      const owned = users.find((a) => a.id === id);
      const found = owned ?? [polly, agent("operator-agent")].find((a) => a.id === id);
      return found
        ? response({ ...found, user_owned: !!owned })
        : response({ error: { message: "Not found", code: "not_found" } }, 404);
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

  it("opens a bookmark with one detail request and no list scans", async () => {
    mount("/settings/custom-agents/a1");
    expect(await screen.findByRole("heading", { name: "research" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete agent" })).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/agents/a1", expect.anything());
  });

  it("keeps an operator-added server agent read-only on a cold link", async () => {
    mount("/settings/custom-agents/operator-agent");
    expect(await screen.findByRole("heading", { name: "operator-agent" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete agent" })).toBeNull();
    expect(screen.getByText("Server-provided agents are read-only.")).toBeVisible();
  });

  it("disables deletion when an older detail response omits ownership", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(response(agent("a1")));
    mount("/settings/custom-agents/a1");
    expect(await screen.findByRole("heading", { name: "a1" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete agent" })).toBeNull();
    expect(screen.getByText(/Update the server to enable agent deletion/)).toBeVisible();
  });

  it("shows loading while the detail request is pending", async () => {
    let resolve!: (value: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    mount("/settings/custom-agents/a1");
    expect(screen.getByText("Loading agent…")).toBeVisible();
    resolve(response({ ...agent("a1"), user_owned: true }));
    expect(await screen.findByRole("heading", { name: "a1" })).toBeVisible();
  });

  it("reports a missing bookmark without fetching lists or retrying 404", async () => {
    mount("/settings/custom-agents/missing");
    expect(
      await screen.findByText(
        "Agent not found. It may have been removed or belong to another user.",
      ),
    ).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /Load more/ })).toBeNull();
  });

  it("retries a failed detail request without fetching lists", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      response({ error: { message: "Unavailable" } }, 500),
    );
    mount("/settings/custom-agents/a1");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this agent");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "research" })).toBeVisible();
    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url)).toEqual([
      "/v1/agents/a1",
      "/v1/agents/a1",
    ]);
  });

  it.each([405, 501])(
    "shows an update notice if the advertised endpoint returns %s",
    async (status) => {
      vi.mocked(authenticatedFetch).mockResolvedValue(
        response({ detail: "Not implemented" }, status),
      );
      mount("/settings/custom-agents/a1");
      expect(await screen.findByRole("alert")).toHaveTextContent("Update the server and reload");
      expect(screen.queryByRole("button", { name: "Delete agent" })).toBeNull();
      expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    },
  );

  it("opens an agent whose ID needs URL encoding", async () => {
    users = [agent("agent/space %2F?#é", "encoded-agent")];
    mount();
    fireEvent.click(await screen.findByRole("link", { name: "encoded-agent" }));
    expect(await screen.findByRole("heading", { name: "encoded-agent" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete agent" })).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledWith(
      `/v1/agents/${encodeURIComponent(users[0].id)}`,
      expect.anything(),
    );
  });

  it("updates search results when the term or loaded pages change", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.searchParams.get("scope") !== "user") return response(page([polly]));
      return response(
        url.searchParams.has("after")
          ? page([agent("later", "research-later")])
          : page(users, true, "a2"),
      );
    });
    mount();
    expect(await screen.findAllByRole("link", { name: "research" })).toHaveLength(2);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: " RESEARCH " } });
    expect(screen.queryByRole("link", { name: /Polly description/ })).toBeNull();
    expect(screen.getAllByRole("link", { name: "research" })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Load more agents" }));
    expect(await screen.findByRole("link", { name: "research-later" })).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "polly" } });
    expect(screen.getByRole("link", { name: /Polly description/ })).toBeVisible();
    expect(screen.queryByRole("link", { name: "research-later" })).toBeNull();
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

  it.each([undefined, false])("disables every entry point when agent_detail=%s", (detail) => {
    flags.detail = detail;
    for (const path of [
      "/settings/custom-agents",
      "/settings/custom-agents/new",
      "/settings/custom-agents/a1",
    ]) {
      const view = mount(path);
      expect(screen.getByRole("alert")).toHaveTextContent("Update the server and reload");
      expect(screen.queryByRole("searchbox")).toBeNull();
      expect(screen.queryByRole("button", { name: /Save agent|Delete agent/ })).toBeNull();
      expect(authenticatedFetch).not.toHaveBeenCalled();
      view.unmount();
    }
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
