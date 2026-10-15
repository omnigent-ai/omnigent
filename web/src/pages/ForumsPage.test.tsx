import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ForumsPage } from "./ForumsPage";
import { forumRequest, listForums } from "@/lib/forumsApi";
import type { Forum } from "@/lib/forumsApi";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "local" }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      {
        host_id: "host",
        name: "My host",
        status: "online",
        configured_harnesses: { "codex-native": true, "claude-native": false },
      },
    ],
  }),
  useHostModelOptions: () => ({ data: [{ id: "model", displayName: "Model one" }] }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  fetchAgentCatalog: async () => [
    { id: "codex", harness: "codex-native", display_name: "Codex" },
    { id: "claude", harness: "claude-native", display_name: "Claude" },
  ],
}));
vi.mock("@/lib/forumsApi", () => ({ forumRequest: vi.fn(), listForums: vi.fn(async () => []) }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => <p>{children}</p>,
}));

const forum: Forum = {
  id: "forum",
  title: "Review parser",
  workspace: "/shared",
  permission_level: 4,
  status: "running",
  max_responses: 100,
  completed: 2,
  active_index: 0,
  participants: [0, 1].map((i) => ({
    session_id: `agent-${i}`,
    harness: "codex-native",
    model: "model",
    name: `${i + 1}. codex-native / model`,
    error: null,
    retry_at: 0,
  })),
  posts: [
    { id: "human", author: "alice", role: "user", text: "Review this parser", created_at: 1 },
    {
      id: "agent",
      author: "1. codex-native / model",
      role: "assistant",
      text: "Consider empty inputs",
      created_at: 2,
    },
  ],
};
const caches: QueryClient[] = [];
function open(path: string) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  caches.push(cache);
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/forums" element={<ForumsPage />} />
          <Route path="/forums/new" element={<ForumsPage />} />
          <Route path="/forums/:forumId" element={<ForumsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.mocked(forumRequest).mockReset());
afterEach(() => {
  cleanup();
  caches.splice(0).forEach((cache) => cache.clear());
});

describe("Forum", () => {
  it("offers an empty list and entry to creation", async () => {
    open("/forums");
    expect(await screen.findByText(/No forums yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New forum" })).toHaveAttribute("href", "/forums/new");
  });

  it("requires two instances, permits identical pairs, and submits their order", async () => {
    vi.mocked(forumRequest).mockResolvedValue({ ...forum, status: "blank", posts: [] });
    open("/forums/new");
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "host" } });
    fireEvent.change(screen.getByLabelText("Shared workspace"), { target: { value: "/shared" } });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Model one" }));
    expect(screen.getByRole("button", { name: "Create forum" })).toBeDisabled();
    expect(screen.queryByText(/Claude ·/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Add another/ }));
    expect(screen.getByText("1. codex-native / model")).toBeInTheDocument();
    expect(screen.getByText("2. codex-native / model")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Display name for participant 1"), {
      target: { value: " Reviewer " },
    });
    expect(screen.getByLabelText("Display name for participant 2")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Create forum" }));
    await waitFor(() =>
      expect(forumRequest).toHaveBeenCalledWith("", {
        host_id: "host",
        workspace: "/shared",
        max_responses: 100,
        participants: [
          { agent_id: "codex", harness: "codex-native", model: "model", display_name: "Reviewer" },
          { agent_id: "codex", harness: "codex-native", model: "model" },
        ],
      }),
    );
    expect(await screen.findByText("What should the agents work on?")).toBeInTheDocument();
  });

  it("attributes posts and sends Stop through its own action", async () => {
    vi.mocked(forumRequest).mockImplementation(async (_path, body) =>
      body ? { ...forum, status: "stopped" } : forum,
    );
    open("/forums/forum");
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("Consider empty inputs")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(forumRequest).toHaveBeenCalledWith("/forum/stop", {}));
    expect(await screen.findByText(/fresh response allowance/)).toBeInTheDocument();
  });

  it("saves a human name without changing the account identity", async () => {
    const named = {
      ...forum,
      posts: [{ ...forum.posts[0], author: "local", display_name: "Hossein" }],
    };
    vi.mocked(forumRequest).mockImplementation(async (_path, body) =>
      body ? named : { ...forum, posts: [] },
    );
    open("/forums/forum");
    const name = await screen.findByRole("textbox", { name: "Your name" });
    fireEvent.change(screen.getByRole("textbox", { name: "Message to the forum" }), {
      target: { value: "Review this parser" },
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "Hossein" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(forumRequest).toHaveBeenCalledWith(
        "/forum/messages",
        expect.objectContaining({ display_name: "Hossein", text: "Review this parser" }),
      ),
    );
    expect(await screen.findByText("Hossein")).toBeInTheDocument();
    expect(screen.queryByText("local")).not.toBeInTheDocument();
    expect(screen.queryByText("Human")).not.toBeInTheDocument();
  });

  it("restores the current user's saved name", async () => {
    vi.mocked(forumRequest).mockResolvedValue({
      ...forum,
      posts: [{ ...forum.posts[0], author: "local", display_name: "Hossein" }],
    });
    open("/forums/forum");
    expect(await screen.findByRole("textbox", { name: "Your name" })).toHaveValue("Hossein");
  });

  it.each([
    ["running", 0, "1. codex-native / model is thinking…"],
    ["running", null, "Preparing the next agent…"],
    ["paused", null, "Waiting for at least two active, available agents…"],
  ] as const)("shows activity for %s with active index %s", async (status, active_index, label) => {
    vi.mocked(forumRequest).mockResolvedValue({ ...forum, status, active_index });
    open("/forums/forum");
    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it.each(["stopped", "limit", "blank"] as const)(
    "does not show a stale thinking indicator when %s",
    async (status) => {
      vi.mocked(forumRequest).mockResolvedValue({ ...forum, status });
      open("/forums/forum");
      await screen.findByText("Review parser");
      expect(screen.queryByText(/is thinking|Preparing the next agent/)).not.toBeInTheDocument();
    },
  );

  it("pauses and resumes the entire forum beside Stop", async () => {
    let status: Forum["status"] = "running";
    vi.mocked(forumRequest).mockImplementation(async (_path, body) => {
      if (body) status = (body as { paused: boolean }).paused ? "suspended" : "running";
      return { ...forum, status };
    });
    open("/forums/forum");
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Paused — resume when you’re ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() =>
      expect(forumRequest).toHaveBeenCalledWith("/forum/pause", { paused: false }),
    );
    expect(await screen.findByRole("button", { name: "Pause" })).toBeEnabled();
  });

  it("pauses and resumes a participant independently", async () => {
    let paused = false;
    vi.mocked(forumRequest).mockImplementation(async (_path, body) => {
      if (body) paused = (body as { paused: boolean }).paused;
      return {
        ...forum,
        participants: forum.participants.map((p, i) => ({ ...p, paused: i === 0 && paused })),
      };
    });
    open("/forums/forum");
    fireEvent.click(await screen.findByRole("button", { name: "Pause 1. codex-native / model" }));
    await waitFor(() =>
      expect(forumRequest).toHaveBeenCalledWith("/forum/participants/agent-0/pause", {
        paused: true,
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Resume 1. codex-native / model" }));
    await waitFor(() =>
      expect(forumRequest).toHaveBeenCalledWith("/forum/participants/agent-0/pause", {
        paused: false,
      }),
    );
  });

  it("requires confirmation before deleting and returns to the list", async () => {
    vi.mocked(forumRequest).mockResolvedValue(forum);
    open("/forums/forum");
    fireEvent.click(await screen.findByRole("button", { name: "Delete forum" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(forumRequest).not.toHaveBeenCalledWith("/forum/delete", {});
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete forum" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    await waitFor(() => expect(forumRequest).toHaveBeenCalledWith("/forum/delete", {}));
    expect(await screen.findByRole("heading", { name: "Forums" })).toBeInTheDocument();
  });

  it("prevents read-only collaborators from posting or stopping", async () => {
    vi.mocked(forumRequest).mockResolvedValue({ ...forum, permission_level: 1 });
    open("/forums/forum");
    await screen.findByText("Review parser");
    expect(screen.getByRole("button", { name: "Rename forum: Review parser" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Delete forum" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Message to the forum" })).toBeDisabled();
  });
});

it("shows fresh consensus progress excluding inactive participants", async () => {
  vi.mocked(forumRequest).mockResolvedValue({ ...forum, completion_votes: ["agent-0"] });
  open("/forums/forum");
  expect(await screen.findByText("1 of 2 agents ready to finish")).toBeInTheDocument();
});

it("explains unanimous completion and keeps human restart available", async () => {
  vi.mocked(forumRequest).mockResolvedValue({
    ...forum,
    status: "stopped",
    stop_reason: "consensus",
    active_index: null,
    posts: [{ ...forum.posts[1], completion_vote: "DONE" }],
  });
  open("/forums/forum");
  expect(await screen.findByText("Stopped by unanimous agreement")).toBeInTheDocument();
  expect(screen.getByText("Ready to finish")).toBeInTheDocument();
  expect(screen.getByText(/fresh response allowance/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
});

it("renames a forum by clicking its title and supports cancel", async () => {
  vi.mocked(forumRequest).mockImplementation(async (_path, body) =>
    body ? { ...forum, title: "Parser review" } : forum,
  );
  open("/forums/forum");
  fireEvent.click(await screen.findByRole("button", { name: "Rename forum: Review parser" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Forum name" }), { target: { value: " " } });
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("textbox", { name: "Forum name" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Rename forum: Review parser" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Forum name" }), {
    target: { value: " Parser review " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(forumRequest).toHaveBeenCalledWith("/forum/rename", { title: "Parser review" }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("textbox", { name: "Forum name" })).not.toBeInTheDocument(),
  );
});

it("keeps titles in the Forums list as navigation links", async () => {
  vi.mocked(listForums).mockResolvedValueOnce([forum]);
  open("/forums");
  expect(await screen.findByRole("link", { name: /Review parser/ })).toHaveAttribute(
    "href",
    "/forums/forum",
  );
  expect(screen.queryByRole("textbox", { name: "Forum name" })).not.toBeInTheDocument();
});
