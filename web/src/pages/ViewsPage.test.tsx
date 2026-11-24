import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/workViewsApi";
import { ViewsPage } from "./ViewsPage";
import { WorkTasksPage } from "./WorkTasksPage";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ work_views: true }) }));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [
      { id: "a".repeat(32), name: "Release" },
      { id: null, name: "Legacy" },
    ],
  }),
}));
vi.mock("@/lib/workViewsApi", async (original) => ({
  ...(await original<typeof api>()),
  listWorkViews: vi.fn(),
  getWorkView: vi.fn(),
  saveWorkView: vi.fn(),
  deleteWorkView: vi.fn(),
  queryWorkView: vi.fn(),
  getWorkTask: vi.fn(),
  saveWorkTask: vi.fn(),
  linkWorkSession: vi.fn(),
  unlinkWorkSession: vi.fn(),
}));

const record: api.ViewRecord = {
  id: "b".repeat(32),
  title: "Ship release",
  status: "todo",
  priority: 2,
  created_at: 10,
  updated_at: 10,
};
const sessionRecords = [
  { ...record, id: "c".repeat(32), title: "Implementation" },
  { ...record, id: "d".repeat(32), title: "Review" },
];
let saved: api.SavedWorkView;
let task: api.WorkTask;

function renderPage(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/work-tasks" element={<WorkTasksPage />} />
          <Route path="/views" element={<ViewsPage />} />
          <Route path="/views/:viewId" element={<ViewsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  saved = { ...api.newView(), name: "My work", id: "saved", created_at: 1, updated_at: 1 };
  task = {
    ...record,
    title: record.title,
    description: "",
    status: "todo",
    priority: 2,
    project_id: null,
    sessions: [],
  };
  vi.mocked(api.listWorkViews).mockImplementation(async () => ({
    data: [saved],
    has_more: false,
    next_cursor: null,
  }));
  vi.mocked(api.getWorkView).mockImplementation(async () => saved);
  vi.mocked(api.saveWorkView).mockImplementation(async (value) => {
    saved = { ...saved, ...value };
    return saved;
  });
  vi.mocked(api.queryWorkView).mockResolvedValue({
    data: [record],
    has_more: false,
    next_cursor: null,
  });
  vi.mocked(api.getWorkTask).mockImplementation(async () => task);
  vi.mocked(api.saveWorkTask).mockImplementation(async (value) => {
    task = { ...task, ...value };
    return task;
  });
  vi.mocked(api.linkWorkSession).mockImplementation(async (_, id, involvement) => {
    task = {
      ...task,
      sessions: [
        ...task.sessions,
        {
          session_id: id,
          title: sessionRecords.find((session) => session.id === id)!.title,
          status: "idle",
          involvement,
        },
      ],
    };
    return { linked: true };
  });
  vi.mocked(api.unlinkWorkSession).mockImplementation(async (_, id) => {
    task = { ...task, sessions: task.sessions.filter((session) => session.session_id !== id) };
    return { linked: false };
  });
  vi.mocked(api.deleteWorkView).mockResolvedValue({ deleted: true });
});
afterEach(cleanup);

it("saves and reopens filters and display independently of membership", async () => {
  const user = userEvent.setup();
  renderPage("/views/new");
  await screen.findByText("Ship release");
  fireEvent.change(screen.getByLabelText("View name"), { target: { value: "Review queue" } });
  await user.click(screen.getByRole("button", { name: "Filters" }));
  await user.click(screen.getByRole("button", { name: "In review" }));
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({ filters: expect.objectContaining({ statuses: ["review"] }) }),
      null,
      expect.any(AbortSignal),
    ),
  );
  const requestsBeforeLayout = vi.mocked(api.queryWorkView).mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Display options" }));
  await user.click(screen.getByRole("button", { name: "Board" }));
  await user.keyboard("{Escape}");
  expect(screen.getByTestId("view-results")).toHaveAttribute("aria-label", "tasks board");
  expect(api.queryWorkView).toHaveBeenCalledTimes(requestsBeforeLayout);
  await user.click(screen.getByRole("button", { name: "Create view" }));
  await waitFor(() => expect(saved.display.layout).toBe("board"));
  expect(saved.filters.statuses).toEqual(["review"]);
  await user.click(screen.getByRole("link", { name: "Views" }));
  await user.click(await screen.findByRole("link", { name: "Review queue" }));
  expect(await screen.findByTestId("view-results")).toHaveAttribute("aria-label", "tasks board");
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
});

it("resets incompatible filters when selecting a different entity", async () => {
  const user = userEvent.setup();
  saved.filters.priorities = [1];
  saved.filters.statuses = ["todo"];
  renderPage("/views/saved");
  await screen.findByLabelText("View name");
  await user.click(screen.getByRole("button", { name: "Sessions" }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        entity_type: "sessions",
        filters: expect.objectContaining({ statuses: [], priorities: [] }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
  await user.click(screen.getByRole("button", { name: "Projects" }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        entity_type: "projects",
        display: expect.objectContaining({ group_by: "none" }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
});

it("opens a project's tasks from an unsaved project view", async () => {
  vi.mocked(api.queryWorkView).mockImplementation(async (definition) => ({
    data:
      definition.entity_type === "projects"
        ? [{ ...record, id: "a".repeat(32), title: "Release" }]
        : [],
    has_more: false,
    next_cursor: null,
  }));
  const user = userEvent.setup();
  renderPage("/views/new?type=projects");
  await user.click(await screen.findByRole("button", { name: /Release/ }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        entity_type: "tasks",
        filters: expect.objectContaining({ project_ids: ["a".repeat(32)] }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
  expect(screen.getByLabelText("View name")).toHaveValue("All tasks");
});

it("creates a task from Tasks without a saved view, project or sessions, then links multiple sessions", async () => {
  const user = userEvent.setup();
  vi.mocked(api.queryWorkView).mockImplementation(async (definition) => ({
    data:
      definition.entity_type === "sessions"
        ? sessionRecords
        : vi.mocked(api.saveWorkTask).mock.calls.length
          ? [task]
          : [],
    has_more: false,
    next_cursor: null,
  }));
  renderPage("/work-tasks");
  expect(screen.getByRole("heading", { name: "Tasks" })).toBeInTheDocument();
  expect(await screen.findByText("No matching tasks")).toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "View item type" })).not.toBeInTheDocument();
  expect(api.listWorkViews).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "New task" }));
  fireEvent.change(await screen.findByLabelText("Task title"), {
    target: { value: "Release checks" },
  });
  await user.click(screen.getByRole("button", { name: "Create task" }));
  await screen.findByRole("button", { name: "Save task" });
  expect(api.saveWorkTask).toHaveBeenCalledWith(
    expect.objectContaining({ title: "Release checks", project_id: null }),
    undefined,
  );
  expect(screen.getByText("No sessions linked yet.")).toBeInTheDocument();
  expect(api.linkWorkSession).not.toHaveBeenCalled();
  await user.click(screen.getByRole("combobox", { name: "Task project" }));
  expect(screen.queryByRole("option", { name: "Legacy" })).toBeNull();
  await user.click(screen.getByRole("option", { name: "Release" }));
  await user.click(screen.getByRole("button", { name: "Save task" }));
  await waitFor(() => expect(task.project_id).toBe("a".repeat(32)));
  await user.click(screen.getByRole("button", { name: "Link session" }));
  fireEvent.change(screen.getByLabelText("Session contribution"), {
    target: { value: "Implementation pass" },
  });
  await user.click(await screen.findByRole("button", { name: "Implementation" }));
  expect(await screen.findByRole("link", { name: "Implementation" })).toHaveAttribute(
    "href",
    `/c/${"c".repeat(32)}`,
  );
  expect(api.linkWorkSession).toHaveBeenCalledWith(task.id, "c".repeat(32), "Implementation pass");
  await user.click(screen.getByRole("button", { name: "Link session" }));
  await user.click(await screen.findByRole("button", { name: "Review" }));
  expect(await screen.findByRole("link", { name: "Review" })).toHaveAttribute(
    "href",
    `/c/${"d".repeat(32)}`,
  );
  expect(task.sessions).toHaveLength(2);
  expect(task.status).toBe("todo");
  await user.click(screen.getByRole("button", { name: "Unlink Implementation" }));
  await waitFor(() => expect(screen.queryByRole("link", { name: "Implementation" })).toBeNull());
  expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Unlink Review" }));
  expect(await screen.findByText("No sessions linked yet.")).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
  await user.click(await screen.findByRole("button", { name: /Release checks/ }));
  expect(await screen.findByLabelText("Task title")).toHaveValue("Release checks");
});

it.each(["/views/new?task=new", "/views/saved?task=new"])(
  "allows editing existing tasks but not creating them from %s",
  async (path) => {
    const user = userEvent.setup();
    renderPage(path);
    const taskRow = await screen.findByRole("button", { name: /Ship release/ });
    expect(screen.queryByRole("button", { name: "New task" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(taskRow);
    expect(await screen.findByLabelText("Task title")).toHaveValue("Ship release");
    fireEvent.change(screen.getByLabelText("Task title"), { target: { value: "Updated release" } });
    await user.click(screen.getByRole("button", { name: "Save task" }));
    await waitFor(() =>
      expect(api.saveWorkTask).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Updated release" }),
        task.id,
      ),
    );
  },
);

it("filters tasks and changes their display without creating a saved view", async () => {
  const user = userEvent.setup();
  renderPage("/work-tasks");
  await screen.findByText("Ship release");
  await user.click(screen.getByRole("button", { name: "Filters" }));
  await user.click(screen.getByRole("button", { name: "In review" }));
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        entity_type: "tasks",
        filters: expect.objectContaining({ statuses: ["review"] }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
  await user.click(screen.getByRole("button", { name: "Display options" }));
  await user.click(screen.getByRole("button", { name: "Board" }));
  await user.keyboard("{Escape}");
  expect(screen.getByTestId("view-results")).toHaveAttribute("aria-label", "tasks board");
  expect(api.saveWorkView).not.toHaveBeenCalled();
});

it("keeps library failures visible and deletes only after confirmation", async () => {
  const user = userEvent.setup();
  vi.mocked(api.listWorkViews).mockRejectedValueOnce(new Error("Could not load views"));
  renderPage("/views");
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load views");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  await user.click(await screen.findByRole("button", { name: "Delete My work" }));
  expect(api.deleteWorkView).not.toHaveBeenCalled();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete view" }));
  await waitFor(() => expect(api.deleteWorkView).toHaveBeenCalledWith("saved"));
});
