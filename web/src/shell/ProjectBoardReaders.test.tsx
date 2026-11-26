import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getProject, updateProjectConfig } from "@/lib/projectsApi";
import { ProjectBoardReadersSection } from "./ProjectSharing";

const SELF = "a".repeat(32);
const SISTER = "b".repeat(32);
const UNSEEN = "c".repeat(32);

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  listProjectShares: vi.fn(),
  revokeProjectShare: vi.fn(),
  setProjectShareRole: vi.fn(),
  shareProject: vi.fn(),
  updateProjectConfig: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [
      { id: "a".repeat(32), name: "Home" },
      { id: "b".repeat(32), name: "Sister factory" },
    ],
  }),
}));

function renderAs(readers: string[], shared: boolean) {
  vi.mocked(getProject).mockResolvedValue({
    id: SELF,
    name: "Home",
    shared,
    config: { board_readers: readers, product_approvers: ["pm@example.com"] },
  } as Awaited<ReturnType<typeof getProject>>);
  vi.mocked(updateProjectConfig).mockResolvedValue({} as Awaited<ReturnType<typeof getProject>>);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ProjectBoardReadersSection projectId={SELF} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProjectBoardReadersSection", () => {
  it("lets the owner add a project by name and keeps the rest of the config", async () => {
    renderAs([], false);
    expect(await screen.findByText("No other project's Lead can read it.")).toBeTruthy();
    fireEvent.change(screen.getByTestId("board-reader-input"), {
      target: { value: "Sister factory" },
    });
    fireEvent.click(screen.getByTestId("board-reader-add"));
    await waitFor(() =>
      expect(updateProjectConfig).toHaveBeenCalledWith(SELF, {
        board_readers: [SISTER],
        product_approvers: ["pm@example.com"],
      }),
    );
  });

  it("refuses an unknown name and the project itself", async () => {
    renderAs([], false);
    const input = await screen.findByTestId("board-reader-input");
    fireEvent.change(input, { target: { value: "Nobody's" } });
    fireEvent.click(screen.getByTestId("board-reader-add"));
    expect((await screen.findByRole("alert")).textContent).toContain("Paste its project ID");
    fireEvent.change(input, { target: { value: SELF } });
    fireEvent.click(screen.getByTestId("board-reader-add"));
    expect((await screen.findByRole("alert")).textContent).toContain("its own board");
    expect(updateProjectConfig).not.toHaveBeenCalled();
  });

  it("revokes a reader, showing unseen projects by ID", async () => {
    renderAs([SISTER, UNSEEN], false);
    expect(await screen.findByText(UNSEEN)).toBeTruthy();
    expect(screen.getByText(/a project you can.t open/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop Sister factory reading this board" }));
    await waitFor(() =>
      expect(updateProjectConfig).toHaveBeenCalledWith(SELF, {
        board_readers: [UNSEEN],
        product_approvers: ["pm@example.com"],
      }),
    );
  });

  it("shows members the list read-only", async () => {
    renderAs([SISTER], true);
    expect(await screen.findByText("Sister factory")).toBeTruthy();
    expect(screen.queryByTestId("board-reader-input")).toBeNull();
    expect(screen.queryByRole("button", { name: /Stop .* reading this board/ })).toBeNull();
  });
});
