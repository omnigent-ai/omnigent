import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ShareProjectModal } from "./ShareProjectModal";

vi.mock("@/lib/projectShareApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/projectShareApi")>();
  return {
    ...actual,
    getProjectShareStatus: vi.fn(),
    listProjectMembers: vi.fn(),
    shareProject: vi.fn(),
    unshareProject: vi.fn(),
  };
});

import * as api from "@/lib/projectShareApi";
const statusMock = vi.mocked(api.getProjectShareStatus);
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

function status(over: Partial<api.ProjectShareStatus> = {}): api.ProjectShareStatus {
  return {
    project: "Proj",
    members: false,
    public: false,
    manageable_count: 2,
    shared_count: 0,
    total_count: 2,
    viewer_is_member: false,
    ...over,
  };
}

beforeEach(() => {
  statusMock.mockReset();
  membersMock.mockReset();
  shareMock.mockReset();
  unshareMock.mockReset();
  membersMock.mockResolvedValue([]);
});

afterEach(cleanup);

describe("ShareProjectModal", () => {
  it("turning on 'all members' shares the __members__ sentinel", async () => {
    statusMock.mockResolvedValue(status());
    shareMock.mockResolvedValue(status({ members: true, shared_count: 2 }));

    render(<ShareProjectModal projectName="Proj" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    const toggle = await screen.findByTestId("project-members-toggle");
    await waitFor(() => expect(toggle).not.toBeDisabled());
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(shareMock).toHaveBeenCalledWith("Proj", api.MEMBERS_USER, 1);
    });
  });

  it("turning off the public link revokes the __public__ sentinel", async () => {
    statusMock.mockResolvedValue(status({ public: true }));
    unshareMock.mockResolvedValue(status({ public: false }));

    render(<ShareProjectModal projectName="Proj" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    const toggle = await screen.findByTestId("project-public-toggle");
    await waitFor(() => expect(toggle).toBeChecked());
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(unshareMock).toHaveBeenCalledWith("Proj", api.PUBLIC_USER);
    });
  });

  it("inviting a user grants them across the project at the chosen level", async () => {
    statusMock.mockResolvedValue(status());
    shareMock.mockResolvedValue(status({ shared_count: 2 }));

    render(<ShareProjectModal projectName="Proj" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    const input = await screen.findByPlaceholderText("alice@example.com");
    fireEvent.change(input, { target: { value: "bob@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /grant/i }));

    await waitFor(() => {
      expect(shareMock).toHaveBeenCalledWith("Proj", "bob@example.com", 1);
    });
  });

  it("lists real members but not sentinels or owners", async () => {
    statusMock.mockResolvedValue(status({ members: true }));
    membersMock.mockResolvedValue([
      { user_id: "alice", level: 4, session_count: 2 },
      { user_id: api.MEMBERS_USER, level: 1, session_count: 2 },
      { user_id: "bob", level: 1, session_count: 2 },
    ]);

    render(<ShareProjectModal projectName="Proj" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());
    // Owner (alice) and the __members__ sentinel are not rendered as rows.
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
    expect(screen.queryByText(api.MEMBERS_USER)).not.toBeInTheDocument();
  });
});
