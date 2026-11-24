import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/workViewsApi";
import { ViewsPage } from "./ViewsPage";
import { WorkTasksPage } from "./WorkTasksPage";
import { WorkTaskPage } from "./WorkTaskPage";
import { peekNewSessionHandoff } from "@/lib/newSessionHandoff";

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

function SessionSetupProbe() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  return (
    <pre data-testid="session-setup">
      {JSON.stringify({
        task: params.get("work_task"),
        project: params.get("project"),
        prompt: peekNewSessionHandoff(params.get("handoff"))?.text,
      })}
    </pre>
  );
}

function renderPage(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/" element={<SessionSetupProbe />} />
          <Route path="/work-tasks" element={<WorkTasksPage />} />
          <Route path="/work-tasks/:taskId" element={<WorkTaskPage />} />
          <Route path="/views" element={<ViewsPage />} />
          <Route path="/views/:viewId" element={<ViewsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, client };
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

it("keeps views grouped by type in the library and carries that type into New view", async () => {
  const views: api.SavedWorkView[] = [
    saved,
    { ...saved, ...api.newView("projects"), id: "projects", name: "Release overview" },
    { ...saved, ...api.newView("sessions"), id: "sessions", name: "Needs attention" },
  ];
  vi.mocked(api.listWorkViews).mockResolvedValue({
    data: views,
    has_more: false,
    next_cursor: null,
  });
  const user = userEvent.setup();
  renderPage("/views");
  expect(await screen.findByRole("link", { name: "My work" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Release overview" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Projects" }));
  expect(screen.getByRole("link", { name: "Release overview" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "My work" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Sessions" }));
  expect(screen.getByRole("link", { name: "Needs attention" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Release overview" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "New view" }));
  expect(await screen.findByRole("button", { name: "Sessions" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByLabelText("View name")).toHaveValue("All sessions");
});

it("saves and reopens filters and display independently of membership", async () => {
  const user = userEvent.setup();
  renderPage("/views/new");
  await screen.findByText("Ship release");
  expect(screen.getByRole("group", { name: "View item type" })).toBeInTheDocument();
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
  await screen.findByRole("button", { name: "Save changes" });
  expect(screen.queryByRole("group", { name: "View item type" })).not.toBeInTheDocument();
  expect(saved.filters.statuses).toEqual(["review"]);
  await user.click(screen.getByRole("link", { name: "Views" }));
  await user.click(await screen.findByRole("link", { name: "Review queue" }));
  expect(await screen.findByTestId("view-results")).toHaveAttribute("aria-label", "tasks board");
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  expect(screen.queryByRole("group", { name: "View item type" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Filters (1)" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Display options" })).toBeInTheDocument();
});

it("refreshes a cached view before editing and preserves a draft during background refresh", async () => {
  const user = userEvent.setup();
  const { client } = renderPage("/views/saved");
  expect(await screen.findByLabelText("View name")).toHaveValue("My work");
  await user.click(screen.getByRole("link", { name: "Views" }));
  await screen.findByRole("link", { name: "My work" });
  saved = { ...saved, name: "Updated elsewhere", favorite: true };
  await user.click(screen.getByRole("link", { name: "My work" }));
  await waitFor(() => expect(screen.getByLabelText("View name")).toHaveValue(saved.name));
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("View name"), { target: { value: "My draft" } });
  vi.mocked(api.getWorkView).mockRejectedValueOnce(new Error("Connection lost"));
  await act(() => client.invalidateQueries({ queryKey: ["work-view", saved.id] }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t refresh this view");
  expect(screen.getByLabelText("View name")).toHaveValue("My draft");
  saved = { ...saved, name: "Another remote update" };
  await act(() => client.invalidateQueries({ queryKey: ["work-view", saved.id] }));
  expect(screen.getByLabelText("View name")).toHaveValue("My draft");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled());
  expect(saved.name).toBe("My draft");
  expect(saved.favorite).toBe(true);
});

it.each(["new", "saved"])("locks the %s view's inputs until saving finishes", async (viewId) => {
  let finishSave!: (view: api.SavedWorkView) => void;
  vi.mocked(api.saveWorkView).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishSave = resolve;
      }),
  );
  const user = userEvent.setup();
  renderPage(`/views/${viewId}`);
  fireEvent.change(await screen.findByLabelText("View name"), { target: { value: "Submitted" } });
  await user.click(
    screen.getByRole("button", { name: viewId === "new" ? "Create view" : "Save changes" }),
  );
  expect(screen.getByLabelText("View name")).toBeDisabled();
  expect(screen.getByLabelText("Filter by name")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Display options" })).toBeDisabled();
  saved = { ...saved, ...vi.mocked(api.saveWorkView).mock.calls[0][0] };
  await act(async () => finishSave(saved));
  await waitFor(() => expect(screen.getByLabelText("View name")).toBeEnabled());
  expect(screen.getByLabelText("View name")).toHaveValue("Submitted");
});

it("refreshes a cached task without replacing unsaved edits when its sessions refresh", async () => {
  const user = userEvent.setup();
  const { client } = renderPage(`/work-tasks?task=${task.id}`);
  expect(await screen.findByRole("heading", { name: "Ship release" })).toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "Tasks" }));
  task = { ...task, title: "Updated elsewhere", description: "New context" };
  await user.click(await screen.findByRole("button", { name: /Ship release/ }));
  await screen.findByRole("heading", { name: task.title });
  await user.click(screen.getByRole("button", { name: "Edit task" }));
  expect(screen.getByLabelText("Task description")).toHaveValue("New context");
  fireEvent.change(screen.getByLabelText("Task title"), { target: { value: "My draft" } });
  vi.mocked(api.getWorkTask).mockRejectedValueOnce(new Error("Connection lost"));
  await act(() => client.invalidateQueries({ queryKey: ["work-task", task.id] }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t refresh this task");
  expect(screen.getByLabelText("Task title")).toHaveValue("My draft");
  task = { ...task, title: "Another remote update" };
  await act(() => client.invalidateQueries({ queryKey: ["work-task", task.id] }));
  expect(screen.getByLabelText("Task title")).toHaveValue("My draft");
  await user.click(screen.getByRole("button", { name: "Save task" }));
  await waitFor(() => expect(task.title).toBe("My draft"));
  expect(task.description).toBe("New context");
});

it.each(["new", record.id])("locks task inputs while saving %s", async (taskId) => {
  let finishSave!: (value: api.WorkTask) => void;
  vi.mocked(api.saveWorkTask).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishSave = resolve;
      }),
  );
  const user = userEvent.setup();
  renderPage(`/work-tasks?task=${taskId}`);
  if (taskId !== "new") await user.click(await screen.findByRole("button", { name: "Edit task" }));
  fireEvent.change(await screen.findByLabelText("Task title"), { target: { value: "Submitted" } });
  await user.click(
    screen.getByRole("button", { name: taskId === "new" ? "Create task" : "Save task" }),
  );
  expect(screen.getByLabelText("Task title")).toBeDisabled();
  expect(screen.getByLabelText("Task description")).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Task status" })).toBeDisabled();
  task = { ...task, ...vi.mocked(api.saveWorkTask).mock.calls[0][0] };
  await act(async () => finishSave(task));
  expect(await screen.findByRole("heading", { name: "Submitted" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Edit task" })).toBeEnabled();
});

it("edits grouped filter chips and removes only the selected field before saving", async () => {
  saved.filters.statuses = ["todo", "in_progress", "review"];
  saved.filters.priorities = [1, 2];
  const user = userEvent.setup();
  renderPage("/views/saved");
  const chips = await screen.findByRole("group", { name: "Applied filters" });
  expect(
    within(chips).getByRole("button", {
      name: "Edit Status filter: Todo, In progress, In review",
    }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Filters (2)" })).toBeInTheDocument();
  await user.click(
    within(chips).getByRole("button", { name: "Edit Priority filter: Urgent, High" }),
  );
  const editor = screen.getByRole("dialog", { name: "Priority filter" });
  expect(within(editor).queryByText("Status")).not.toBeInTheDocument();
  await user.click(within(editor).getByRole("button", { name: "High" }));
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "Remove Status filter" }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        filters: expect.objectContaining({ statuses: [], priorities: [1] }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
  expect(screen.queryByRole("button", { name: /Edit Status filter/ })).not.toBeInTheDocument();
  expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();
  expect(api.saveWorkView).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled());
  await user.click(screen.getByRole("link", { name: "Views" }));
  await user.click(await screen.findByRole("link", { name: "My work" }));
  expect(
    await screen.findByRole("button", { name: "Edit Priority filter: Urgent" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Edit Status filter/ })).not.toBeInTheDocument();
});

it("shows and clears project, search and date filters without hiding unavailable projects", async () => {
  saved.filters.project_ids = ["a".repeat(32), "e".repeat(32)];
  saved.filters.no_project = true;
  saved.filters.search = "release";
  saved.filters.updated_after = 0;
  const user = userEvent.setup();
  renderPage("/views/saved");
  await user.click(
    await screen.findByRole("button", {
      name: "Edit Project filter: Release, Unavailable project, No project",
    }),
  );
  await user.click(
    within(screen.getByRole("dialog", { name: "Project filter" })).getByRole("button", {
      name: "No project",
    }),
  );
  await user.keyboard("{Escape}");
  expect(
    screen.getByRole("button", { name: "Edit Project filter: Release, Unavailable project" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove Project filter" }));
  expect(
    screen.getByRole("button", { name: "Edit Updated since filter: 1970-01-01" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove Updated since filter" }));
  await user.click(screen.getByRole("button", { name: "Edit Search filter: release" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Search filter" }), {
    target: { value: "updated" },
  });
  await user.keyboard("{Escape}");
  expect(screen.getByLabelText("Filter by name")).toHaveValue("updated");
  await user.click(screen.getByRole("button", { name: "Remove Search filter" }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({ filters: api.newView().filters }),
      null,
      expect.any(AbortSignal),
    ),
  );
  expect(screen.getByText("No filters")).toBeInTheDocument();
});

it("keeps the archived-session scope visible and removes incompatible status selections", async () => {
  saved = { ...saved, ...api.newView("sessions") };
  saved.filters.statuses = ["failed", "archived"];
  saved.filters.include_archived = true;
  const user = userEvent.setup();
  renderPage("/views/saved");
  expect(
    await screen.findByRole("button", { name: "Edit Archived filter: Included" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove Archived filter" }));
  await waitFor(() =>
    expect(api.queryWorkView).toHaveBeenLastCalledWith(
      expect.objectContaining({
        filters: expect.objectContaining({ statuses: ["failed"], include_archived: false }),
      }),
      null,
      expect.any(AbortSignal),
    ),
  );
  await user.click(screen.getByRole("button", { name: "Edit Archived filter: Excluded" }));
  await user.click(screen.getByRole("switch", { name: "Include archived" }));
  await user.keyboard("{Escape}");
  expect(
    screen.getByRole("button", { name: "Edit Archived filter: Included" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Edit Status filter: Failed" })).toBeInTheDocument();
});

it("resets incompatible filters when selecting a different entity for a new view", async () => {
  const user = userEvent.setup();
  renderPage("/views/new");
  await screen.findByLabelText("View name");
  await user.click(screen.getByRole("button", { name: "Filters" }));
  await user.click(screen.getByRole("button", { name: "Todo" }));
  await user.click(screen.getByRole("button", { name: "Urgent" }));
  await user.keyboard("{Escape}");
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
  expect(screen.getByRole("dialog", { name: "New task" })).toBeInTheDocument();
  fireEvent.change(await screen.findByLabelText("Task title"), {
    target: { value: "Release checks" },
  });
  await user.click(screen.getByRole("button", { name: "Create task" }));
  await screen.findByRole("heading", { name: "Release checks" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.saveWorkTask).toHaveBeenCalledWith(
    expect.objectContaining({ title: "Release checks", project_id: null }),
    undefined,
  );
  expect(screen.getByText("No sessions linked yet.")).toBeInTheDocument();
  expect(api.linkWorkSession).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Edit task" }));
  await user.click(screen.getByRole("combobox", { name: "Task project" }));
  expect(screen.queryByRole("option", { name: "Legacy" })).toBeNull();
  await user.click(screen.getByRole("option", { name: "Release" }));
  await user.click(screen.getByRole("button", { name: "Save task" }));
  await waitFor(() => expect(task.project_id).toBe("a".repeat(32)));
  await user.click(await screen.findByRole("button", { name: "Link session" }));
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
  await user.click(screen.getByRole("link", { name: "Tasks" }));
  await user.click(await screen.findByRole("button", { name: /Release checks/ }));
  expect(await screen.findByRole("heading", { name: "Release checks" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
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
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
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

it.each([null, "Release"])("starts a session with task context and project %s", async (project) => {
  task = {
    ...task,
    description: "Check the rollout",
    project_id: project ? "a".repeat(32) : null,
    project_name: project,
  };
  const user = userEvent.setup();
  renderPage(`/work-tasks/${task.id}`);
  await user.click(await screen.findByRole("button", { name: "Start session" }));
  expect(JSON.parse(screen.getByTestId("session-setup").textContent!)).toEqual({
    task: task.id,
    project,
    prompt: "Ship release\n\nCheck the rollout",
  });
  expect(api.saveWorkTask).not.toHaveBeenCalled();
});

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
