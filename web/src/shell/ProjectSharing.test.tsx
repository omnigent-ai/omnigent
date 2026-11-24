import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyText } from "@/lib/clipboard";
import { getProject, listProjectShares } from "@/lib/projectsApi";
import { ProjectSharingSection } from "./ProjectSharing";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  listProjectShares: vi.fn(),
  revokeProjectShare: vi.fn(),
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
