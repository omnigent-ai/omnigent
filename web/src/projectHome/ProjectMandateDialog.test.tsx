import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectsApi";
import { ProjectMandateDialog } from "./ProjectMandateDialog";

const projects = vi.hoisted(() => ({
  data: [] as { id: string | null; name: string }[],
  isLoading: false,
  error: null as Error | null,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({ useProjects: () => projects }));
vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  createProject: vi.fn(),
  getProject: vi.fn(),
  updateProjectMandate: vi.fn(),
}));

const clients: QueryClient[] = [];
function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  const onClose = vi.fn();
  render(<ProjectMandateDialog projectName="Shipping" onClose={onClose} />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  return { onClose };
}

beforeEach(() => {
  vi.resetAllMocks();
  projects.data = [{ id: "project", name: "Shipping" }];
  projects.isLoading = false;
  projects.error = null;
  vi.mocked(api.getProject).mockResolvedValue({
    id: "project",
    name: "Shipping",
    mandate: "Only edit docs/.",
  });
  vi.mocked(api.updateProjectMandate).mockImplementation(async (id, mandate) => ({
    id,
    name: "Shipping",
    mandate: mandate.trim() || null,
  }));
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Project mandate editor", () => {
  it("edits the stored mandate and saves only that field", async () => {
    const { onClose } = renderDialog();
    const input = await screen.findByLabelText("Project mandate");
    expect(input).toHaveValue("Only edit docs/.");
    await userEvent.clear(input);
    await userEvent.type(input, "Never push.");
    await userEvent.click(screen.getByTestId("project-mandate-save"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(api.updateProjectMandate).toHaveBeenCalledWith("project", "Never push.");
  });

  it("creates a label-only folder's project row before saving", async () => {
    projects.data = [];
    vi.mocked(api.createProject).mockResolvedValue({ id: "created", name: "Shipping" });
    const { onClose } = renderDialog();
    const input = await screen.findByLabelText("Project mandate");
    expect(input).toHaveValue("");
    expect(api.getProject).not.toHaveBeenCalled();
    await userEvent.type(input, "Stay in docs/.");
    await userEvent.click(screen.getByTestId("project-mandate-save"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(api.createProject).toHaveBeenCalledWith("Shipping");
    expect(api.updateProjectMandate).toHaveBeenCalledWith("created", "Stay in docs/.");
  });

  it("counts trimmed UTF-8 bytes and blocks an oversized save", async () => {
    renderDialog();
    const input = await screen.findByLabelText("Project mandate");
    fireEvent.change(input, { target: { value: `  ${"é".repeat(6_000)}  ` } });
    expect(screen.getByTestId("project-mandate-size")).toHaveTextContent("12,000 / 12,000 bytes");
    expect(screen.getByTestId("project-mandate-save")).toBeEnabled();
    fireEvent.change(input, { target: { value: "é".repeat(6_001) } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("project-mandate-save")).toBeDisabled();
  });

  it("keeps the draft and shows the server's error when saving fails", async () => {
    vi.mocked(api.updateProjectMandate).mockRejectedValue(new Error("project mandate too large"));
    const { onClose } = renderDialog();
    const input = await screen.findByLabelText("Project mandate");
    await userEvent.type(input, " More.");
    await userEvent.click(screen.getByTestId("project-mandate-save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("project mandate too large");
    expect(input).toHaveValue("Only edit docs/. More.");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not offer a blank editor when the mandate fails to load", async () => {
    vi.mocked(api.getProject).mockRejectedValue(new Error("Project not found"));
    renderDialog();
    expect(await screen.findByRole("alert")).toHaveTextContent("Project not found");
    expect(screen.queryByLabelText("Project mandate")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
