import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ShareProjectModal } from "./ShareProjectModal";

vi.mock("@/lib/projectsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/projectsApi")>();
  return {
    ...actual,
    listProjectMembers: vi.fn(),
    shareProject: vi.fn(),
    unshareProject: vi.fn(),
  };
});

import * as api from "@/lib/projectsApi";
const membersMock = vi.mocked(api.listProjectMembers);
const shareMock = vi.mocked(api.shareProject);
const unshareMock = vi.mocked(api.unshareProject);

function createWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <TooltipProvider>{children}</TooltipProvider>
      </QueryClientProvider>
    );
  };
}

function renderModal() {
  render(
    <ShareProjectModal projectId="proj_x" projectName="Proj" open={true} onOpenChange={() => {}} />,
    { wrapper: createWrapper() },
  );
}

beforeEach(() => {
  membersMock.mockReset();
  shareMock.mockReset();
  unshareMock.mockReset();
  membersMock.mockResolvedValue([]);
});

afterEach(cleanup);

describe("ShareProjectModal", () => {
  it("turning on 'all members' shares the __members__ sentinel", async () => {
    shareMock.mockResolvedValue({
      id: "proj_x",
      name: "Proj",
      created_by: "alice",
      created_at: 0,
      updated_at: 0,
      permission_level: 4,
      members: true,
      public: false,
    });
    renderModal();
    const toggle = await screen.findByTestId("project-members-toggle");
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);
    await waitFor(() => expect(shareMock).toHaveBeenCalledWith("proj_x", api.MEMBERS_USER, 1));
  });

  it("turning off the public link revokes the __public__ sentinel", async () => {
    membersMock.mockResolvedValue([{ user_id: api.PUBLIC_USER, level: 1 }]);
    unshareMock.mockResolvedValue(undefined);
    renderModal();
    const toggle = await screen.findByTestId("project-public-toggle");
    await waitFor(() => expect(toggle).toBeChecked());
    fireEvent.click(toggle);
    await waitFor(() => expect(unshareMock).toHaveBeenCalledWith("proj_x", api.PUBLIC_USER));
  });

  it("inviting a user grants them at the chosen level", async () => {
    shareMock.mockResolvedValue({
      id: "proj_x",
      name: "Proj",
      created_by: "alice",
      created_at: 0,
      updated_at: 0,
      permission_level: 4,
      members: false,
      public: false,
    });
    renderModal();
    const input = await screen.findByPlaceholderText("alice@example.com");
    fireEvent.change(input, { target: { value: "bob@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /grant/i }));
    await waitFor(() => expect(shareMock).toHaveBeenCalledWith("proj_x", "bob@example.com", 1));
  });

  it("lists real members but not sentinels or owners", async () => {
    membersMock.mockResolvedValue([
      { user_id: "alice", level: 4 },
      { user_id: api.MEMBERS_USER, level: 1 },
      { user_id: "bob", level: 1 },
    ]);
    renderModal();
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
    expect(screen.queryByText(api.MEMBERS_USER)).not.toBeInTheDocument();
  });
});
