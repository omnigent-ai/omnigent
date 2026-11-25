import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
const viewer = vi.hoisted(() => ({ id: "member@example.com" as string | null }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => viewer.id }));
vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  createProject: vi.fn(),
  getProject: vi.fn(),
  updateProjectMandate: vi.fn(),
  acceptMandateProposal: vi.fn(),
  dismissMandateProposal: vi.fn(),
}));

const PROPOSAL = { id: "proposal", text: "Attach a video to every change.", created_at: 1 };

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

/** An existing mandate opens rendered; switch to the text editor. */
async function openEditor() {
  await userEvent.click(await screen.findByTestId("project-mandate-edit-tab"));
  return screen.getByLabelText("Project mandate");
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
  it("opens an existing mandate rendered as markdown, proposals included", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate:
        "## Scope\n\n- Only edit **docs/**.\n- Run `tsc` before [review](https://example.com).",
      mandate_proposals: [{ ...PROPOSAL, text: "Attach a **video** to every change." }],
    });
    renderDialog();
    const preview = await screen.findByTestId("project-mandate-preview");
    expect(screen.queryByLabelText("Project mandate")).toBeNull();
    expect(preview.querySelector("[data-streamdown=heading-2]")).toHaveTextContent("Scope");
    expect(preview.querySelectorAll("[data-streamdown=list-item]")).toHaveLength(2);
    expect(preview.querySelector("[data-streamdown=strong]")).toHaveTextContent("docs/");
    expect(preview.querySelector("[data-streamdown=inline-code]")).toHaveTextContent("tsc");
    expect(preview.querySelector("[data-streamdown=link]")).toHaveAttribute(
      "href",
      "https://example.com/",
    );
    const proposal = screen.getByTestId("project-mandate-proposal");
    expect(proposal.querySelector("[data-streamdown=strong]")).toHaveTextContent("video");
    const input = await openEditor();
    expect(input).toHaveValue(
      "## Scope\n\n- Only edit **docs/**.\n- Run `tsc` before [review](https://example.com).",
    );
    await userEvent.type(input, " Never push.");
    await userEvent.click(screen.getByTestId("project-mandate-preview-tab"));
    expect(screen.getByTestId("project-mandate-preview")).toHaveTextContent("Never push.");
  });

  it("edits the stored mandate and saves only that field", async () => {
    const { onClose } = renderDialog();
    const input = await openEditor();
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
    const input = await openEditor();
    const size = screen.getByTestId("project-mandate-size");
    fireEvent.change(input, { target: { value: `  ${"é".repeat(8_000)}  ` } });
    expect(size).toHaveTextContent(/^16,000 \/ 32,000 bytes$/);
    fireEvent.change(input, { target: { value: "é".repeat(16_000) } });
    expect(size).toHaveTextContent(
      "32,000 / 32,000 bytes · Every session in this project loads the mandate, about 8,000 tokens each.",
    );
    expect(screen.getByTestId("project-mandate-save")).toBeEnabled();
    fireEvent.change(input, { target: { value: "é".repeat(16_001) } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(size).not.toHaveTextContent("tokens");
    expect(screen.getByTestId("project-mandate-save")).toBeDisabled();
  });

  it("keeps the draft and shows the server's error when saving fails", async () => {
    vi.mocked(api.updateProjectMandate).mockRejectedValue(new Error("project mandate too large"));
    const { onClose } = renderDialog();
    const input = await openEditor();
    await userEvent.type(input, " More.");
    await userEvent.click(screen.getByTestId("project-mandate-save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("project mandate too large");
    expect(input).toHaveValue("Only edit docs/. More.");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("lists proposals and applies them only when the owner accepts one", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.",
      mandate_proposals: [PROPOSAL],
    });
    vi.mocked(api.acceptMandateProposal).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.\n\nAttach a video to every change.",
      mandate_proposals: [],
    });
    renderDialog();
    const input = await openEditor();
    expect(screen.getByTestId("project-mandate-proposal")).toHaveTextContent(PROPOSAL.text);
    expect(input).toHaveValue("Only edit docs/.");
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(input).toHaveValue("Only edit docs/.\n\nAttach a video to every change."),
    );
    expect(api.acceptMandateProposal).toHaveBeenCalledWith("project", "proposal");
    expect(screen.queryByTestId("project-mandate-proposals")).toBeNull();
    expect(api.updateProjectMandate).not.toHaveBeenCalled();
  });

  it("keeps unsaved edits when accepting, so saving keeps the accepted text", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.",
      mandate_proposals: [PROPOSAL],
    });
    vi.mocked(api.acceptMandateProposal).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.\n\nAttach a video to every change.",
      mandate_proposals: [],
    });
    const { onClose } = renderDialog();
    const input = await openEditor();
    await userEvent.type(input, " Never push.");
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(input).toHaveValue("Only edit docs/. Never push.\n\nAttach a video to every change."),
    );
    await userEvent.click(screen.getByTestId("project-mandate-save"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(api.updateProjectMandate).toHaveBeenCalledWith(
      "project",
      "Only edit docs/. Never push.\n\nAttach a video to every change.",
    );
  });

  it("dismisses a proposal without changing the mandate", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.",
      mandate_proposals: [PROPOSAL],
    });
    vi.mocked(api.dismissMandateProposal).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.",
      mandate_proposals: [],
    });
    renderDialog();
    const input = await openEditor();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByTestId("project-mandate-proposals")).toBeNull());
    expect(api.dismissMandateProposal).toHaveBeenCalledWith("project", "proposal");
    expect(input).toHaveValue("Only edit docs/.");
  });

  it("shows a section replacement as a diff and applies it on accept", async () => {
    const replacement = {
      id: "replace",
      text: "## Push\n\nPush Verified work.",
      created_at: 1,
      mode: "replace_section" as const,
      section: "Push",
      current_text: "## Push\n\nAsk first.",
      stale: false,
    };
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "## Push\n\nAsk first.",
      mandate_proposals: [replacement],
    });
    vi.mocked(api.acceptMandateProposal).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "## Push\n\nPush Verified work.",
      mandate_proposals: [],
    });
    renderDialog();
    const proposal = await screen.findByTestId("project-mandate-proposal");
    expect(proposal).toHaveTextContent("Replace section “Push”");
    const diff = screen.getByTestId("project-mandate-proposal-diff");
    expect(diff.querySelector("[data-kind=removed]")).toHaveTextContent("Ask first.");
    expect(diff.querySelector("[data-kind=added]")).toHaveTextContent("Push Verified work.");
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(api.acceptMandateProposal).toHaveBeenCalledWith("project", "replace");
    const input = await openEditor();
    expect(input).toHaveValue("## Push\n\nPush Verified work.");
  });

  it("blocks accepting a stale replacement, or any replacement over unsaved edits", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      mandate: "Only edit docs/.",
      mandate_proposals: [
        {
          id: "stale",
          text: "Everything new.",
          created_at: 1,
          mode: "replace_all",
          current_text: "Only edit docs/.",
          stale: true,
        },
        {
          id: "fresh",
          text: "",
          created_at: 2,
          mode: "replace_section",
          section: "Git",
          current_text: "## Git",
          stale: false,
        },
      ],
    });
    renderDialog();
    const [stale, fresh] = await screen.findAllByTestId("project-mandate-proposal");
    expect(stale).toHaveTextContent("Replace the whole mandate");
    expect(screen.getByTestId("project-mandate-proposal-stale")).toHaveTextContent("Out of date");
    const accept = (card: HTMLElement) => within(card).getByRole("button", { name: "Accept" });
    expect(accept(stale!)).toBeDisabled();
    expect(fresh).toHaveTextContent("Delete section “Git”");
    expect(accept(fresh!)).toBeEnabled();
    await userEvent.type(await openEditor(), " More.");
    expect(accept(fresh!)).toBeDisabled();
    expect(fresh).toHaveTextContent("Save or cancel your edits");
  });

  it("does not offer a blank editor when the mandate fails to load", async () => {
    vi.mocked(api.getProject).mockRejectedValue(new Error("Project not found"));
    renderDialog();
    expect(await screen.findByRole("alert")).toHaveTextContent("Project not found");
    expect(screen.queryByLabelText("Project mandate")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("Project mandate for a shared member", () => {
  it("shows the mandate and proposals read-only, with no edit, save or accept", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      shared: true,
      mandate: "Only edit docs/.",
      mandate_proposals: [PROPOSAL],
    });
    renderDialog();
    expect(await screen.findByTestId("project-mandate-read-only")).toHaveTextContent(
      "Only the project owner or a product approver can change the Mandate.",
    );
    expect(screen.getByTestId("project-mandate-preview")).toHaveTextContent("Only edit docs/.");
    expect(screen.getByTestId("project-mandate-proposal")).toHaveTextContent(PROPOSAL.text);
    expect(screen.queryByTestId("project-mandate-edit-tab")).toBeNull();
    expect(screen.queryByTestId("project-mandate-save")).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("lets a member who is a product approver edit and accept", async () => {
    vi.mocked(api.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      shared: true,
      mandate: "Only edit docs/.",
      mandate_proposals: [PROPOSAL],
      config: { product_approvers: ["member@example.com"] },
    });
    renderDialog();
    expect(await screen.findByTestId("project-mandate-edit-tab")).toBeTruthy();
    expect(screen.getByTestId("project-mandate-save")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByTestId("project-mandate-read-only")).toBeNull();
  });

  it("opens a blank mandate as an empty preview instead of the editor", async () => {
    vi.mocked(api.getProject).mockResolvedValue({ id: "project", name: "Shipping", shared: true });
    renderDialog();
    expect(await screen.findByText("No mandate yet.")).toBeTruthy();
    expect(screen.queryByLabelText("Project mandate")).toBeNull();
  });
});
