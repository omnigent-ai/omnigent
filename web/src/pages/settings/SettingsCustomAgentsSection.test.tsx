import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsCustomAgentsSection } from "./SettingsCustomAgentsSection";
import { authenticatedFetch } from "@/lib/identity";
import { buildAgentBundle } from "@/lib/agentBundle";
import type { AgentConfig } from "@/lib/agentsApi";
import type { ManagedAgent } from "@/hooks/useCustomAgents";

const flags = vi.hoisted(() => ({
  feature: true,
  install: true,
  detail: true as boolean | undefined,
  edit: true as boolean | undefined,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    features: { custom_agents_settings_ui: flags.feature },
    agent_install: flags.install,
    agent_detail: flags.detail,
    agent_config_edit: flags.edit,
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
  flags.edit = true;
  failSave = false;
  failDelete = false;
  inUse = false;
  users = [agent("a1", "research"), agent("a2", "research")];
  vi.mocked(buildAgentBundle)
    .mockReset()
    .mockResolvedValue(new File(["bundle"], "agent.tar.gz"));
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
    if (url.pathname.endsWith("/config")) {
      const id = decodeURIComponent(url.pathname.split("/").at(-2)!);
      const found = users.find((item) => item.id === id)!;
      return new Response(JSON.stringify({ ...editableConfig, ...found, mcp_servers: {} }), {
        headers: { ETag: '"original"' },
      });
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
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Custom agents settings", () => {
  it("keeps duplicate names by ID and reviews server agents without mutation actions", async () => {
    mount();
    expect(await screen.findAllByRole("link", { name: "research" })).toHaveLength(2);
    for (const row of users) {
      expect(screen.getByText(`${row.id} · v${row.version}`)).toBeVisible();
    }
    expect(screen.queryByRole("link", { name: /jcode/ })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: /Polly description/ }));
    expect(await screen.findByRole("heading", { name: "Polly" })).toBeVisible();
    expect(screen.getByText("Review code")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete agent" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("opens a bookmark with direct detail and configuration requests and no list scans", async () => {
    mount("/settings/custom-agents/a1");
    expect(await screen.findByRole("heading", { name: "research" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete agent" })).toBeVisible();
    await screen.findByLabelText("Instructions");
    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url)).toEqual([
      "/v1/agents/a1",
      "/v1/agents/a1/config",
    ]);
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
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    mount("/settings/custom-agents/a1");
    expect(screen.getByText("Loading agent…")).toBeVisible();
    resolve(response({ ...agent("a1"), user_owned: true }));
    expect(await screen.findByRole("heading", { name: "a1" })).toBeVisible();
    expect(await screen.findByLabelText("Instructions")).toHaveValue(" literal\n");
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
      "/v1/agents/a1/config",
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

const editableConfig: AgentConfig = {
  id: "a1",
  name: "research",
  version: 1,
  description: "Original",
  instructions: " literal\n",
  model: null,
  harness: "claude-sdk",
  mcp_servers: {},
};

function configResponses({
  fail = 0,
  etag = true,
  patchEtag = etag,
}: { fail?: number; etag?: boolean; patchEtag?: boolean } = {}) {
  const previous = vi.mocked(authenticatedFetch).getMockImplementation()!;
  let current = { ...editableConfig };
  let patchStatus = fail;
  vi.mocked(authenticatedFetch).mockImplementation(async (url, init) => {
    if (init?.method === "PATCH") {
      if (patchStatus)
        return response(
          { error: { code: "precondition_failed", message: "Save failed" } },
          patchStatus,
        );
      const patch = JSON.parse(init.body as string);
      current = { ...current, ...patch, version: current.version + 1 };
      users[0] = { ...users[0], description: current.description, version: current.version };
      return new Response(JSON.stringify(current), { headers: patchEtag ? { ETag: '"new"' } : {} });
    }
    if (String(url).endsWith("/config"))
      return new Response(JSON.stringify(current), { headers: etag ? { ETag: '"original"' } : {} });
    return previous(url, init);
  });
  return {
    recover: () => {
      patchStatus = 0;
      current = { ...current, description: "Concurrent winner" };
    },
  };
}

async function openEditor() {
  mount("/settings/custom-agents/a1");
  await screen.findByDisplayValue("Original");
  expect(screen.getByLabelText("Name")).toHaveAttribute("readonly");
  expect(screen.queryByRole("dialog")).toBeNull();
}

describe("editing custom agents", () => {
  it("adds an MCP server without native crypto.randomUUID", async () => {
    vi.stubGlobal("crypto", {
      getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
    });
    configResponses();
    await openEditor();
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    expect(screen.getByRole("textbox", { name: "Server name" })).toBeVisible();
  });
  it.each([false, undefined])("keeps old servers read-only for edits: %s", async (edit) => {
    flags.edit = edit;
    mount("/settings/custom-agents/a1");
    await screen.findByRole("button", { name: "Delete agent" });
    expect(screen.queryByRole("form", { name: "Agent configuration" })).toBeNull();
    expect(authenticatedFetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/config"),
      expect.anything(),
    );
  });
  it.each([
    [true, true],
    [false, false],
    [true, false],
  ])("saves only changed fields twice with GET ETag=%s, PATCH ETag=%s", async (etag, patchEtag) => {
    configResponses({ etag, patchEtag });
    await openEditor();
    expect(screen.queryByRole("dialog")).toBeNull();
    const description = screen.getByLabelText("Description");
    expect(description).not.toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    fireEvent.change(description, { target: { value: "Updated" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByLabelText("Description")).toBe(description);
    expect(description).toHaveValue("Updated");
    fireEvent.change(description, { target: { value: "Updated again" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(description).toHaveValue("Updated again");
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/agents/a1",
      expect.objectContaining({
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(patchEtag ? { "If-Match": '"new"' } : {}),
        },
        body: '{"description":"Updated again"}',
      }),
    );
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/agents/a1",
      expect.objectContaining({
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(etag ? { "If-Match": '"original"' } : {}),
        },
        body: '{"description":"Updated"}',
      }),
    );
    expect(buildAgentBundle).not.toHaveBeenCalled();
  });
  it("preserves new edits when the detail refresh after saving fails and is retried", async () => {
    configResponses();
    await openEditor();
    const previous = vi.mocked(authenticatedFetch).getMockImplementation()!;
    let finishRefresh!: (value: Response) => void;
    const pendingDetail = new Promise<Response>((resolve) => {
      finishRefresh = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation((url, init) =>
      String(url) === "/v1/agents/a1" && init?.method !== "PATCH"
        ? pendingDetail
        : previous(url, init),
    );
    const description = screen.getByLabelText("Description");
    fireEvent.change(description, { target: { value: "Saved change" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    fireEvent.change(description, { target: { value: "Unsaved second draft" } });

    vi.mocked(authenticatedFetch).mockImplementation(previous);
    finishRefresh(response({ error: { message: "Unavailable" } }, 500));
    const warning = await screen.findByRole("alert");
    expect(screen.getByLabelText("Description")).toBe(description);
    expect(description).toHaveValue("Unsaved second draft");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Custom agents" })).toBeVisible();
    expect(warning).toHaveTextContent("Could not refresh this agent");

    fireEvent.click(within(warning).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByLabelText("Description")).toBe(description);
    expect(description).toHaveValue("Unsaved second draft");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
  it("retains a failed draft and confirms cancellation", async () => {
    configResponses({ fail: 500 });
    await openEditor();
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "keep me" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
    expect(screen.getByLabelText("Instructions")).toHaveValue("keep me");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Instructions")).toHaveValue("keep me");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByRole("heading", { name: "Custom agents" })).toBeVisible();
    fireEvent.click((await screen.findAllByRole("link", { name: "research" }))[0]);
    expect(await screen.findByLabelText("Instructions")).toHaveValue(" literal\n");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });
  it("confirms a dirty draft before returning to the list", async () => {
    configResponses();
    await openEditor();
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Unsaved" } });
    fireEvent.click(screen.getByRole("button", { name: "Custom agents" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved");
    fireEvent.click(screen.getByRole("button", { name: "Custom agents" }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("heading", { name: "Custom agents" })).toBeVisible();
    expect(
      vi.mocked(authenticatedFetch).mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);
  });
  it("keeps a conflicted draft until explicit reload, then allows editing the latest revision", async () => {
    const server = configResponses({ fail: 412 });
    await openEditor();
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "My draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your draft is still here");
    expect(screen.getByLabelText("Description")).toHaveValue("My draft");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    server.recover();
    fireEvent.click(screen.getByRole("button", { name: "Reload latest" }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard changes" }));
    expect(await screen.findByDisplayValue("Concurrent winner")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });
  it("preserves the draft on 428 and allows retrying after the server updates", async () => {
    const server = configResponses({ etag: false, fail: 428 });
    await openEditor();
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "New" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This server needs an update");
    expect(screen.getByRole("alert")).toHaveTextContent("Your draft is still here");
    expect(screen.getByLabelText("Description")).toHaveValue("New");
    expect(screen.queryByRole("button", { name: "Reload latest" })).toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    server.recover();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Description")).toHaveValue("New");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });
});
