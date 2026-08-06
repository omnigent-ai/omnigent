// Layout regression tests for the project-folder header's icon/chevron.
// Desired behaviour: a project folder shows its folder icon by default and,
// on desktop hover/focus, swaps that folder icon for a chevron *in place*
// (rather than trailing the name). On mobile (no hover) the folder icon
// stays put and the trailing chevron is shown instead. Plain section headers
// with no leading icon keep the old behaviour: a trailing chevron revealed on
// desktop hover/focus. These tests lock that structure in:
//   1. The project header renders the folder icon and an overlaid chevron in
//      the icon slot; the folder fades out and the chevron fades in on
//      desktop hover/focus.
//   2. The project header's trailing chevron is mobile-only (`md:hidden`).
//   3. A header without a leading icon (the "Projects" group header) keeps a
//      hover-revealed trailing chevron and does NOT swap an icon.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  // One project so a folder header renders. Empty projects are not filtered
  // out, so no conversations are needed to exercise the header layout.
  useProjects: () => ({ data: [{ id: "p_my", name: "My Project" }] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** The <button> header for a section/folder, found by its accessible name. */
function headerButton(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

/** SVG elements expose `className` as an SVGAnimatedString, not a string;
 *  read the raw class attribute instead. */
function classOf(el: Element): string {
  return el.getAttribute("class") ?? "";
}

beforeEach(() => {
  mockConversations([]);
});

afterEach(() => {
  cleanup();
});

describe("project folder header icon/chevron", () => {
  it("shows the folder icon and overlays a chevron that swaps on desktop hover/focus", () => {
    renderSidebar();
    const header = headerButton("My Project");

    // Project folders are real rows, not muted section labels: use the shared
    // text-ui compact treatment with 8px insets/gap and foreground text.
    expect(header).toHaveClass(
      "sidebar-row",
      "h-auto",
      "min-h-0",
      "gap-2",
      "rounded-[var(--radius-otto-button)]",
      "px-2",
      "py-1.5",
      "md:py-1",
      "sidebar-compact-text",
      "text-foreground",
    );

    const folder = header.querySelector(".lucide-folder") as HTMLElement;
    expect(folder).not.toBeNull();
    expect(folder).toHaveClass("text-muted-foreground");

    // The folder icon sits in a wrapper that fades out on desktop hover/focus.
    const folderWrapper = folder.parentElement as HTMLElement;
    expect(classOf(folderWrapper)).toMatch(/md:group-hover:opacity-0/);
    expect(classOf(folderWrapper)).toMatch(/md:group-focus-visible:opacity-0/);

    // A chevron shares the icon slot (absolute), hidden by default and fading
    // in on desktop hover/focus so it takes the folder's place.
    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    const swap = chevrons.find((c) => classOf(c).includes("absolute")) as HTMLElement;
    expect(swap).toBeTruthy();
    expect(classOf(swap)).toMatch(/opacity-0/);
    expect(classOf(swap)).toMatch(/md:group-hover:opacity-100/);
    expect(classOf(swap)).toMatch(/md:group-focus-visible:opacity-100/);

    // The swap chevron lives in the same icon slot as the folder (not trailing
    // the title), so it overlays the folder position.
    expect(swap.parentElement).toBe(folderWrapper.parentElement);
  });

  it("keeps the project header's trailing chevron mobile-only", () => {
    renderSidebar();
    const header = headerButton("My Project");

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    // Two chevrons: the in-slot swap (absolute) and the trailing one.
    const trailing = chevrons.find((c) => !classOf(c).includes("absolute")) as HTMLElement;
    expect(trailing).toBeTruthy();
    // Trailing chevron is hidden on desktop (the swap replaces it there) and
    // shown on mobile where there's no hover.
    expect(classOf(trailing)).toMatch(/md:hidden/);
    expect(classOf(trailing)).not.toMatch(/md:group-hover:opacity-100/);
  });

  it("matches row height for the centered empty-project container", () => {
    renderSidebar();
    fireEvent.click(headerButton("My Project"));

    // Both the flat session list and the expanded project render "No sessions";
    // this assertion is about the project's indented dashed well.
    const empty = screen
      .getAllByText("No sessions")
      .find((el) => el.classList.contains("sidebar-row"))!;
    expect(empty).toHaveClass(
      "ml-8",
      "mr-2",
      "sidebar-row",
      "h-auto",
      "min-h-0",
      "py-1.5",
      "md:py-1",
      "justify-center",
      "rounded-[var(--radius-otto-button)]",
      "border",
      "border-dashed",
      "text-center",
      "text-ui",
    );
    // Body tier, not the smaller caption step.
    expect(empty).not.toHaveClass("min-h-9", "text-sm");
  });

  it("leaves iconless section headers with a hover-revealed trailing chevron and no swap", () => {
    renderSidebar();
    // The "Projects" group header carries no leading icon.
    const header = headerButton("Projects");

    // The parent section label uses the settings-scaled subtitle tier.
    expect(header).toHaveClass("gap-1", "pb-2", "pl-2", "text-sm", "font-normal");
    expect(header).not.toHaveClass("font-medium", "uppercase");

    expect(header.querySelector(".lucide-folder")).toBeNull();

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    // Exactly one chevron (no in-slot swap), and it is the classic
    // desktop-hover-revealed trailing caret — not mobile-only.
    expect(chevrons).toHaveLength(1);
    const [chevron] = chevrons;
    expect(classOf(chevron)).not.toMatch(/\babsolute\b/);
    expect(classOf(chevron)).not.toMatch(/md:hidden/);
    expect(classOf(chevron)).toMatch(/md:group-hover:opacity-100/);
  });
});
