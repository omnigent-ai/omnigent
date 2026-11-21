import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectTicketsApi";
import { createProject } from "@/lib/projectsApi";
import { ProjectTicketStagesDialog } from "./ProjectTicketStagesDialog";

const projects = vi.hoisted(() => ({
  data: [] as { id: string | null; name: string }[],
  isLoading: false,
  error: null as Error | null,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({ useProjects: () => projects }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_ticket_stages: true, project_ticket_lifecycle: true }),
}));
vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getTicketStages: vi.fn(),
  updateTicketStages: vi.fn(),
}));
vi.mock("@/lib/projectsApi", () => ({ createProject: vi.fn() }));

const clients: QueryClient[] = [];
function renderEditor() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  const onClose = vi.fn();
  const element = <ProjectTicketStagesDialog projectName="Shipping" onClose={onClose} />;
  const rendered = render(element, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  return {
    ...rendered,
    client,
    onClose,
    refresh: () =>
      rendered.rerender(<ProjectTicketStagesDialog projectName="Shipping" onClose={onClose} />),
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  projects.data = [{ id: "project", name: "Shipping" }];
  projects.isLoading = false;
  projects.error = null;
  vi.mocked(api.getTicketStages).mockResolvedValue({
    ...structuredClone(api.DEFAULT_TICKET_STAGES),
    occupied_stage_ids: ["planning"],
  });
  vi.mocked(api.updateTicketStages).mockImplementation(
    async (_project, { stages, initial_stage_id }) => ({ stages, initial_stage_id }),
  );
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

describe("Project stage editor", () => {
  it("waits for project lookup and real stage definitions before editing", async () => {
    projects.isLoading = true;
    projects.data = [];
    let resolveStages!: (stages: api.TicketStages) => void;
    vi.mocked(api.getTicketStages).mockReturnValue(
      new Promise((resolve) => {
        resolveStages = resolve;
      }),
    );
    const { refresh } = renderEditor();
    expect(screen.queryByLabelText("Stage name 1")).toBeNull();
    expect(api.getTicketStages).not.toHaveBeenCalled();
    projects.data = [{ id: "project", name: "Shipping" }];
    projects.isLoading = false;
    refresh();
    await waitFor(() => expect(api.getTicketStages).toHaveBeenCalledWith("project", true));
    expect(screen.queryByLabelText("Stage name 1")).toBeNull();
    await act(async () =>
      resolveStages({
        stages: [
          {
            id: "investigating",
            name: "Investigating",
            instructions: "Gather evidence",
            is_complete: false,
          },
        ],
        initial_stage_id: "investigating",
      }),
    );
    expect(await screen.findByLabelText("Stage name 1")).toHaveValue("Investigating");
    expect(screen.getByLabelText("Instructions for Investigating")).toHaveValue("Gather evidence");
    expect(screen.getByRole("button", { name: "Remove Investigating" })).toBeDisabled();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(api.updateTicketStages).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateTicketStages).mock.calls[0][1]).toMatchObject({
      initial_stage_id: "investigating",
      stages: [{ id: "investigating", is_complete: true }],
    });
  });

  it("shows a stage-load error and retries instead of inventing defaults", async () => {
    vi.mocked(api.getTicketStages).mockRejectedValueOnce(new Error("Stages unavailable"));
    renderEditor();
    expect(await screen.findByRole("alert")).toHaveTextContent("Stages unavailable");
    expect(screen.queryByLabelText("Stage name 1")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Stage name 1")).toHaveValue("Open");
  });

  it("previews, saves and resets appearance with keyboard-accessible choices", async () => {
    const user = userEvent.setup();
    const { onClose, unmount } = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Appearance for Open" }));
    const picker = screen.getByRole("dialog", { name: "Appearance for Open" });
    expect(within(picker).getByRole("radio", { name: "Gray" })).toBeChecked();
    expect(within(picker).getByRole("radio", { name: "Gray" })).toHaveFocus();
    expect(within(picker).getByRole("radio", { name: "Dashed circle" })).toBeChecked();
    await user.click(within(picker).getByRole("radio", { name: "Purple" }));
    expect(within(picker).getByRole("radio", { name: "Purple" })).toHaveFocus();
    // Radix moves focus asynchronously while an arrow key is held.
    await user.keyboard("{ArrowRight>}");
    await waitFor(() => expect(within(picker).getByRole("radio", { name: "Pink" })).toBeChecked());
    await user.keyboard("{/ArrowRight}");
    await user.click(within(picker).getByRole("radio", { name: "Purple" }));
    await user.click(within(picker).getByRole("radio", { name: "Search" }));
    expect(within(picker).getByRole("radio", { name: "Search" })).toBeChecked();
    expect(within(picker).getByText("Open").closest('[data-slot="badge"]')).toHaveClass(
      "text-purple-600",
    );
    expect(api.updateTicketStages).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Appearance for Open" })).toBeNull();
    expect(screen.getByRole("button", { name: "Appearance for Open" })).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    const saved = vi.mocked(api.updateTicketStages).mock.calls[0][1];
    expect(saved.stages[0]).toMatchObject({
      id: "open",
      color: "purple",
      icon: "search",
      is_complete: false,
    });
    unmount();

    vi.mocked(api.getTicketStages).mockResolvedValue(saved);
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Appearance for Open" }));
    expect(screen.getByRole("radio", { name: "Purple" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Search" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Reset appearance" }));
    expect(screen.getByRole("radio", { name: "Gray" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Dashed circle" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(api.updateTicketStages).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateTicketStages).mock.calls[1][1].stages[0]).toMatchObject({
      id: "open",
      color: null,
      icon: null,
    });
  });

  it("renames stable IDs and derives completion from the added last stage", async () => {
    const { onClose } = renderEditor();
    fireEvent.change(await screen.findByLabelText("Stage name 1"), { target: { value: " New " } });
    fireEvent.click(screen.getByRole("button", { name: "Add stage" }));
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Stage name 6"), { target: { value: "Resolved" } });
    fireEvent.change(screen.getByLabelText("Instructions for Resolved"), {
      target: { value: " Attach recovery evidence. " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(api.updateTicketStages).toHaveBeenCalledWith("project", {
      stages: [
        { ...api.DEFAULT_TICKET_STAGES.stages[0], name: "New" },
        ...api.DEFAULT_TICKET_STAGES.stages
          .slice(1)
          .map((stage) => ({ ...stage, is_complete: false })),
        {
          id: expect.stringMatching(/^stage-[a-z0-9]{12}$/),
          name: "Resolved",
          instructions: "Attach recovery evidence.",
          is_complete: true,
        },
      ],
      initial_stage_id: "open",
      moves: {},
    });
  });

  it.each([
    { index: 0, key: "ArrowDown", order: ["planning", "open", "in_progress", "review", "done"] },
    { index: 4, key: "ArrowUp", order: ["open", "planning", "in_progress", "done", "review"] },
  ])("derives start and completion after dragging stage $index", async ({ index, key, order }) => {
    const configuration = structuredClone(api.DEFAULT_TICKET_STAGES);
    configuration.stages[index].color = "pink";
    configuration.stages[index].icon = "flag";
    vi.mocked(api.getTicketStages).mockResolvedValue(configuration);
    renderEditor();
    const list = await screen.findByRole("list", { name: "Project stages" });
    const rows = within(list).getAllByRole("listitem");
    const handles = within(list).getAllByRole("button", { name: /^Reorder / });
    rows.forEach((row, rowIndex) =>
      vi
        .spyOn(row, "getBoundingClientRect")
        .mockReturnValue(new DOMRect(0, rowIndex * 100, 500, 90)),
    );
    handles.forEach((handle, handleIndex) =>
      vi
        .spyOn(handle, "getBoundingClientRect")
        .mockReturnValue(new DOMRect(0, handleIndex * 100, 24, 24)),
    );
    vi.spyOn(list, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 500, 500));
    handles[index].focus();
    fireEvent.keyDown(handles[index], { key: " ", code: "Space" });
    await waitFor(() => expect(handles[index]).toHaveAttribute("aria-pressed", "true"));
    fireEvent.keyDown(handles[index], { key, code: key });
    fireEvent.keyDown(handles[index], { key: " ", code: "Space" });
    await waitFor(() => {
      expect(screen.getByLabelText(`Stage name ${index + 1}`)).toHaveValue(
        api.DEFAULT_TICKET_STAGES.stages.find((stage) => stage.id === order[index])?.name,
      );
    });
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(api.updateTicketStages).toHaveBeenCalledOnce());
    const saved = vi.mocked(api.updateTicketStages).mock.calls[0][1];
    expect(saved.stages.map((stage) => stage.id)).toEqual(order);
    expect(saved.initial_stage_id).toBe(order[0]);
    expect(saved.stages.filter((stage) => stage.is_complete).map((stage) => stage.id)).toEqual([
      order.at(-1),
    ]);
    expect(saved.stages.find((stage) => stage.id === configuration.stages[index].id)).toMatchObject(
      {
        color: "pink",
        icon: "flag",
      },
    );
  });

  it("requires a surviving destination for populated removals and supports undo", async () => {
    const user = userEvent.setup();
    renderEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Planning" }));
    expect(screen.getByRole("button", { name: "Undo removal of Planning" })).toHaveTextContent(
      /^Undo$/,
    );
    expect(screen.getByText("Planning", { selector: "strong" })).toBeInTheDocument();
    expect(screen.queryByText(/^Removing /)).toBeNull();
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    await user.click(screen.getByRole("combobox", { name: "Move tickets from Planning to" }));
    await user.click(screen.getByRole("option", { name: "Working" }));
    expect(screen.getByRole("button", { name: "Save stages" })).toBeEnabled();
    await user.click(screen.getByRole("combobox", { name: "Move tickets from Planning to" }));
    await user.click(screen.getByRole("option", { name: "Choose a stage…" }));
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    await user.click(screen.getByRole("combobox", { name: "Move tickets from Planning to" }));
    await user.click(screen.getByRole("option", { name: "Working" }));
    expect(screen.getByRole("button", { name: "Save stages" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove Working" }));
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Undo removal of Working" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(api.updateTicketStages).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateTicketStages).mock.calls[0][1]).toMatchObject({
      initial_stage_id: "open",
      moves: { planning: "in_progress" },
    });
    expect(
      vi.mocked(api.updateTicketStages).mock.calls[0][1].stages.map((stage) => stage.id),
    ).toEqual(["open", "done", "in_progress"]);
  });

  it("rejects blank or duplicate names and cancels without persisting drafts", async () => {
    const user = userEvent.setup();
    const { onClose } = renderEditor();
    const name = await screen.findByLabelText("Stage name 1");
    await user.click(screen.getByRole("button", { name: "Appearance for Open" }));
    await user.click(screen.getByRole("radio", { name: "Purple" }));
    await user.keyboard("{Escape}");
    fireEvent.change(name, { target: { value: " working " } });
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    fireEvent.change(name, { target: { value: " " } });
    expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(api.updateTicketStages).not.toHaveBeenCalled();
  });

  it("keeps dirty fields after a failed save and a usage refresh, then retries", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getTicketStages).mockResolvedValueOnce({
      ...structuredClone(api.DEFAULT_TICKET_STAGES),
      occupied_stage_ids: [],
    });
    vi.mocked(api.updateTicketStages).mockRejectedValueOnce(
      new Error("Choose where occupied stages move"),
    );
    const { onClose } = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Appearance for Open" }));
    await user.click(screen.getByRole("radio", { name: "Purple" }));
    await user.click(screen.getByRole("radio", { name: "Search" }));
    await user.keyboard("{Escape}");
    fireEvent.change(screen.getByLabelText("Stage name 1"), { target: { value: "New" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove Planning" }));
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose where occupied stages move");
    await waitFor(() => expect(api.getTicketStages).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("Stage name 1")).toHaveValue("New");
    expect(onClose).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save stages" })).toBeDisabled());
    await user.click(screen.getByRole("combobox", { name: "Move tickets from Planning to" }));
    await user.click(screen.getByRole("option", { name: "Working" }));
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateTicketStages).mock.calls[1][1].stages[0]).toMatchObject({
      id: "open",
      name: "New",
      color: "purple",
      icon: "search",
    });
  });

  it("creates a label-only project on save and keeps the draft through its ID assignment", async () => {
    projects.data = [{ id: null, name: "Shipping" }];
    vi.mocked(createProject).mockResolvedValue({ id: "created", name: "Shipping" });
    let rejectSave!: (error: Error) => void;
    vi.mocked(api.updateTicketStages).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectSave = reject;
      }),
    );
    const { refresh, onClose } = renderEditor();
    fireEvent.change(await screen.findByLabelText("Stage name 1"), { target: { value: "New" } });
    expect(createProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() =>
      expect(api.updateTicketStages).toHaveBeenCalledWith("created", expect.anything()),
    );
    projects.data = [{ id: "created", name: "Shipping" }];
    refresh();
    await waitFor(() => expect(api.getTicketStages).toHaveBeenCalledWith("created", true));
    expect(screen.getByLabelText("Stage name 1")).toHaveValue("New");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Appearance for New" })).toBeDisabled();
    await act(async () => rejectSave(new Error("Save failed")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
    expect(screen.getByLabelText("Stage name 1")).toHaveValue("New");
    fireEvent.click(screen.getByRole("button", { name: "Save stages" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(createProject).toHaveBeenCalledExactlyOnceWith("Shipping");
  });
});
