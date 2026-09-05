import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import {
  useArchiveConversation,
  useArchivedConversations,
  useArchivedSessionFacets,
  useProjects,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { ArchiveLibraryRail } from "./ArchiveLibraryRail";

vi.mock("@/hooks/useConversations", () => ({
  useArchivedConversations: vi.fn(),
  useArchivedSessionFacets: vi.fn(),
  useProjects: vi.fn(),
  useArchiveConversation: vi.fn(),
  useStopAndDeleteConversation: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: vi.fn(() => false) }));
vi.mock("@/components/archive/ArchiveTranscriptViewer", () => ({
  ArchiveTranscriptViewer: ({ conversation: selected }: { conversation: Conversation | null }) => (
    <div data-testid="transcript-stub">{selected?.title ?? "none"}</div>
  ),
}));

const useArchivedMock = vi.mocked(useArchivedConversations);
const useFacetsMock = vi.mocked(useArchivedSessionFacets);
const useHostsMock = vi.mocked(useHosts);
const useProjectsMock = vi.mocked(useProjects);
const useArchiveMock = vi.mocked(useArchiveConversation);
const useDeleteMock = vi.mocked(useStopAndDeleteConversation);

function renderRail(props: ComponentProps<typeof ArchiveLibraryRail> = {}) {
  return render(
    <TooltipProvider>
      <ArchiveLibraryRail {...props} />
    </TooltipProvider>,
  );
}

function archiveConversation(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1,
    updated_at: 1,
    archived: true,
    labels: {},
    permission_level: null,
    agent_name: "codex",
    host_id: "host-win",
  };
}

describe("ArchiveLibraryRail", () => {
  beforeEach(() => {
    localStorage.clear();
    useArchiveMock.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useArchiveConversation>);
    useDeleteMock.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useStopAndDeleteConversation>);
    useFacetsMock.mockReturnValue({
      data: { projects: [], hostIds: [], agentNames: [] },
    } as unknown as ReturnType<typeof useArchivedSessionFacets>);
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host-win", name: "Windows" }],
    } as ReturnType<typeof useHosts>);
    useProjectsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
    const firstPage = {
      data: {
        data: [archiveConversation("conv-1", "First archive page")],
        first_id: "conv-1",
        last_id: "cursor-1",
        has_more: true,
      },
      isLoading: false,
      isFetching: false,
    } as ReturnType<typeof useArchivedConversations>;
    const secondPage = {
      data: {
        data: [archiveConversation("conv-2", "Second archive page")],
        first_id: "conv-2",
        last_id: "conv-2",
        has_more: false,
      },
      isLoading: false,
      isFetching: false,
    } as ReturnType<typeof useArchivedConversations>;
    useArchivedMock.mockImplementation((_filters, after) => {
      return after === "cursor-1" ? secondPage : firstPage;
    });
  });

  it("pages through a large archive and can return to the prior page", async () => {
    renderRail();

    expect(await screen.findByRole("option", { name: /First archive page/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByRole("option", { name: /Second archive page/ })).toBeInTheDocument();
    expect(screen.getByText("Page 2")).toBeInTheDocument();
    expect(screen.getByTestId("transcript-stub")).toHaveTextContent("Second archive page");

    fireEvent.click(screen.getByRole("button", { name: /Previous/ }));
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /First archive page/ })).toBeInTheDocument(),
    );
    expect(screen.getByText("Page 1")).toBeInTheDocument();
  });

  it("fuzzy-selects a viable first-class project from linked facets", () => {
    useFacetsMock.mockReturnValue({
      data: { projects: ["First class project"], hostIds: [], agentNames: [] },
    } as unknown as ReturnType<typeof useArchivedSessionFacets>);
    useProjectsMock.mockReturnValue({
      data: [{ id: "project-1", name: "First class project" }],
    } as unknown as ReturnType<typeof useProjects>);

    renderRail();

    fireEvent.click(screen.getByRole("combobox", { name: /by project/i }));
    const input = screen.getByPlaceholderText("Search project…");
    fireEvent.change(input, { target: { value: "first class" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("combobox", { name: /by project/i })).toHaveTextContent(
      "First class project",
    );
  });

  it("does not auto-select a sole named facet that may coexist with null rows", async () => {
    useFacetsMock.mockReturnValue({
      data: { projects: ["Only named project"], hostIds: ["host-win"], agentNames: ["codex"] },
    } as unknown as ReturnType<typeof useArchivedSessionFacets>);

    renderRail();

    await waitFor(() => {
      const filters = useArchivedMock.mock.calls.at(-1)?.[0];
      expect(filters?.project).toBeUndefined();
      expect(filters?.hostId).toBeUndefined();
      expect(filters?.agentName).toBeUndefined();
    });
    expect(screen.getByRole("combobox", { name: /by project/i })).toHaveTextContent("All projects");
    expect(screen.getByRole("combobox", { name: /by host/i })).toHaveTextContent("All hosts");
    expect(screen.getByRole("combobox", { name: /by agent/i })).toHaveTextContent("All agents");
  });

  it("seeds project and host filters from the active session", async () => {
    useFacetsMock.mockReturnValue({
      data: { projects: ["Omnigent"], hostIds: ["host-win"], agentNames: [] },
    } as unknown as ReturnType<typeof useArchivedSessionFacets>);

    renderRail({ initialProject: "Omnigent", initialHostId: "host-win" });

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: /by project/i })).toHaveTextContent("Omnigent");
      expect(screen.getByRole("combobox", { name: /by host/i })).toHaveTextContent("Windows");
    });
  });

  it("keeps row actions available without a visible keyboard instruction", async () => {
    renderRail();

    expect(await screen.findByRole("button", { name: "Unarchive session" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete archived session" })).toBeInTheDocument();
    expect(screen.queryByText(/Return open/)).toBeNull();
  });
});
