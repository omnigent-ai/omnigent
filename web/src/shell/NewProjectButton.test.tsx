import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { createProject, type Project } from "@/lib/projectsApi";
import { NewProjectButton } from "./NewProjectButton";

vi.mock("@/lib/projectsApi", () => ({ createProject: vi.fn() }));
vi.mock("@/components/ProjectIconPicker", () => ({ EmojiPicker: () => null }));
afterEach(cleanup);

describe("NewProjectButton", () => {
  it("dismisses before creation finishes and leaves a reopened draft untouched", async () => {
    let finish!: (project: Project) => void;
    const onCreateFailed = vi.fn();
    vi.mocked(createProject).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const onCreated = vi.fn();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <NewProjectButton onCreated={onCreated} onCreateFailed={onCreateFailed} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "First" } });
    fireEvent.keyDown(screen.getByPlaceholderText("Project name…"), { key: "Enter" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onCreated).toHaveBeenCalledWith("First");
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(1));
    expect(client.getQueryData(["projects"])).toEqual([
      expect.objectContaining({ name: "First", pending: "Creating" }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByPlaceholderText("Project name…"), { target: { value: "Second" } });
    await act(async () => finish({ id: "first", name: "First" }));
    expect(screen.getByPlaceholderText("Project name…")).toHaveValue("Second");
    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    vi.mocked(createProject).mockRejectedValueOnce(new Error("offline"));
    fireEvent.keyDown(screen.getByPlaceholderText("Project name…"), { key: "Enter" });
    await waitFor(() => expect(onCreateFailed).toHaveBeenCalledWith("Second"));
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
