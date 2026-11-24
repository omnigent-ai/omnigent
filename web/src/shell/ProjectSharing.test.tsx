import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyText } from "@/lib/clipboard";
import { getProject, listProjectShares, revokeProjectShare } from "@/lib/projectsApi";
import { ProjectSharingSection } from "./ProjectSharing";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  listProjectShares: vi.fn(),
  revokeProjectShare: vi.fn(),
  setProjectShareRole: vi.fn(),
  shareProject: vi.fn(),
  updateProjectConfig: vi.fn(),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));

const projectUrl = `${window.location.origin}/projects/Demo%20board`;

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProjectSharingSection projectId="proj_1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(getProject).mockResolvedValue({
    id: "proj_1",
    name: "Demo board",
    owner_user_id: "owner@example.com",
  } as Awaited<ReturnType<typeof getProject>>);
  vi.mocked(listProjectShares).mockResolvedValue({
    owner_user_id: "owner@example.com",
    data: [],
  } as unknown as Awaited<ReturnType<typeof listProjectShares>>);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProjectSharingSection copy URL", () => {
  it("copies the absolute project URL and confirms", async () => {
    vi.mocked(copyText).mockResolvedValue();
    renderSection();

    fireEvent.click(await screen.findByTestId("project-share-copy-link"));

    expect(copyText).toHaveBeenCalledWith(projectUrl);
    expect(await screen.findByText("Copied")).toBeTruthy();
  });

  it("shows the URL to copy by hand when the clipboard is unavailable", async () => {
    vi.mocked(copyText).mockRejectedValue(new Error("Clipboard API not available"));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    renderSection();

    fireEvent.click(await screen.findByTestId("project-share-copy-link"));

    expect(await screen.findByText(projectUrl)).toBeTruthy();
    expect(screen.getByText("Copy URL")).toBeTruthy();
  });
});

function sharedWith(viewerRole: "manager" | "member") {
  vi.mocked(getProject).mockResolvedValue({
    id: "proj_1",
    name: "Demo board",
    owner_user_id: "owner@example.com",
    shared: true,
  } as Awaited<ReturnType<typeof getProject>>);
  vi.mocked(listProjectShares).mockResolvedValue({
    owner_user_id: "owner@example.com",
    viewer_role: viewerRole,
    data: [
      { user_id: "boss@example.com", created_at: 1, role: "manager" },
      { user_id: "pat@example.com", created_at: 2, role: "member" },
    ],
  });
}

describe("ProjectSharingSection roles", () => {
  it("lets a Manager invite and remove members but not Managers", async () => {
    sharedWith("manager");
    vi.mocked(revokeProjectShare).mockResolvedValue();
    renderSection();

    expect(await screen.findByTestId("project-share-input")).toBeTruthy();
    expect(screen.queryByTestId("project-share-role")).toBeNull();
    const labels = (await screen.findAllByTestId("project-share-role-label")).map(
      (el) => el.textContent,
    );
    expect(labels).toEqual(["Manager", "Member"]);
    const revokes = screen.getAllByTestId("project-share-revoke");
    expect(revokes).toHaveLength(1);
    fireEvent.click(revokes[0] as HTMLElement);
    await waitFor(() =>
      expect(vi.mocked(revokeProjectShare).mock.calls[0]?.[1]).toBe("pat@example.com"),
    );
  });

  it("shows a member the list read-only", async () => {
    sharedWith("member");
    renderSection();

    expect(await screen.findAllByTestId("project-share-role-label")).toHaveLength(2);
    expect(screen.queryByTestId("project-share-input")).toBeNull();
    expect(screen.queryByTestId("project-share-revoke")).toBeNull();
  });

  it("gives the owner a role picker per member", async () => {
    vi.mocked(listProjectShares).mockResolvedValue({
      owner_user_id: "owner@example.com",
      viewer_role: "owner",
      data: [{ user_id: "pat@example.com", created_at: 2, role: "manager" }],
    });
    renderSection();

    const picker = await screen.findByTestId("project-share-role");
    expect(picker.textContent).toContain("Manager");
    expect(screen.getByTestId("project-share-revoke")).toBeTruthy();
  });
});
