// Layout regression tests for the project-folder header's icon/chevron.
// Desired behaviour: a project folder shows its folder icon by default and,
// on hover-capable desktop, swaps that folder icon for a chevron *in place*
// (rather than trailing the name). Without hover at any width, the folder icon
// stays put and the trailing chevron is shown instead. Plain section headers
// with no leading icon follow the same capability gate. These tests lock that
// structure in:
//   1. The project header renders the folder icon and an overlaid chevron in
//      the icon slot; the folder fades out and the chevron fades in on
//      hover-capable desktop.
//   2. The project header's trailing chevron stays visible without hover.
//   3. A header without a leading icon (the "Projects" group header) keeps a
//      capability-gated trailing chevron and does NOT swap an icon.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
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

function renderSidebar(initialEntry = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
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

function stubInputScenario(
  width: number,
  anyCoarse: boolean,
  canHover: boolean,
  coarsePrimary = anyCoarse,
  anyHover = canHover,
) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: query.split(/\s+and\s+/).every((condition) => {
        const normalized = condition.trim();
        if (normalized === "(pointer: coarse)") return coarsePrimary;
        if (normalized === "(any-pointer: coarse)") return anyCoarse;
        if (normalized === "(hover: hover)") return canHover;
        if (normalized === "(any-hover: hover)") return anyHover;
        const min = normalized.match(/^\(min-width: ([\d.]+)px\)$/);
        if (min) return width >= parseFloat(min[1]);
        const max = normalized.match(/^\(max-width: ([\d.]+)px\)$/);
        if (max) return width <= parseFloat(max[1]);
        return false;
      }),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
}

beforeEach(() => {
  stubInputScenario(1280, false, true);
  mockConversations([]);
});

afterEach(() => {
  cleanup();
});

describe("project folder header icon/chevron", () => {
  // These are class-contract tests: jsdom does not evaluate @media (hover:hover).
  // The recorded sidebar-project-row-fixes.gif demo is the behavioral guardrail.
  it("preserves the folder-to-chevron swap on a wide hover-capable device", () => {
    renderSidebar();
    const header = headerButton("My Project");

    expect(window.matchMedia("(min-width: 768px)").matches).toBe(true);
    expect(window.matchMedia("(any-pointer: coarse)").matches).toBe(false);
    expect(window.matchMedia("(hover: hover)").matches).toBe(true);

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

    // Only a hover-capable desktop fades the folder out of its icon slot.
    const folderWrapper = folder.parentElement as HTMLElement;
    expect(folderWrapper).toHaveClass(
      "[@media(hover:hover)]:md:group-hover:opacity-0",
      "[@media(hover:hover)]:md:group-focus-visible:opacity-0",
    );
    expect(folderWrapper).not.toHaveClass("md:group-hover:opacity-0");

    // A chevron shares the icon slot (absolute), hidden by default and fading
    // in on desktop hover/focus so it takes the folder's place.
    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    const swap = chevrons.find((c) => classOf(c).includes("absolute")) as HTMLElement;
    expect(swap).toBeTruthy();
    expect(swap).toHaveClass(
      "hidden",
      "md:flex",
      "opacity-0",
      "[@media(hover:hover)]:md:group-hover:opacity-100",
      "[@media(hover:hover)]:md:group-focus-visible:opacity-100",
    );
    expect(swap).not.toHaveClass("md:group-hover:opacity-100");

    // The swap chevron lives in the same icon slot as the folder (not trailing
    // the title), so it overlays the folder position.
    expect(swap.parentElement).toBe(folderWrapper.parentElement);
  });

  it.each([
    ["narrow", 390],
    ["wide", 1024],
  ])("keeps the trailing caret visible on a %s coarse-pointer device", (_label, width) => {
    stubInputScenario(width, true, false);
    renderSidebar();
    const header = headerButton("My Project");

    expect(window.matchMedia("(any-pointer: coarse)").matches).toBe(true);
    expect(window.matchMedia("(hover: hover)").matches).toBe(false);
    expect(window.matchMedia("(pointer: coarse) and (hover: hover)").matches).toBe(false);
    expect(window.matchMedia("(min-width: 768px)").matches).toBe(width >= 768);

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    const trailing = chevrons.find((c) => !classOf(c).includes("absolute")) as HTMLElement;
    expect(trailing).toBeTruthy();
    expect(trailing).toHaveClass("[@media(hover:hover)]:md:hidden");
    expect(trailing).not.toHaveClass("md:hidden");
  });

  it("highlights the project row instead of global New session for a project-scoped composer", () => {
    renderSidebar("/?project=My%20Project");

    const header = headerButton("My Project");
    expect(header).toHaveAttribute("aria-current", "page");
    expect(header).toHaveClass(
      "bg-[var(--sidebar-active)]",
      "text-[var(--sidebar-active-foreground)]",
    );
    expect(header.querySelector(".lucide-folder")).toHaveClass(
      "text-[var(--sidebar-active-foreground)]",
    );

    const newSession = screen.getByTestId("new-chat-button");
    expect(newSession).not.toHaveClass("bg-[var(--sidebar-active)]");
    expect(newSession.querySelector("svg")).toHaveClass("text-muted-foreground");
  });

  it("shows a left-aligned empty-project message and new-session action", () => {
    renderSidebar();
    fireEvent.click(headerButton("My Project"));

    const action = screen
      .getAllByRole("link", { name: "new session" })
      .find((link) => link.getAttribute("href") === "/?project=My%20Project")!;
    const empty = action.closest(".sidebar-row")!;
    expect(empty).toHaveClass(
      "ml-8",
      "mr-2",
      "sidebar-row",
      "h-auto",
      "min-h-0",
      "px-0",
      "py-1",
      "pb-2",
      "md:py-1",
      "md:pb-2",
      "justify-center",
      "rounded-[var(--radius-otto-button)]",
      "items-start",
      "text-left",
    );
    expect(empty).not.toHaveClass("border", "border-dashed", "items-center", "text-center");
    // Both sentences share one body-tier text block.
    expect(empty).not.toHaveClass("min-h-9", "text-sm");
    const message = empty.querySelector("p")!;
    expect(message).toHaveClass("text-ui", "text-muted-foreground");
    const body = message.querySelector("span")!;
    expect(body).toHaveClass("text-ui");
    expect(body).toHaveTextContent("No sessions. Start a new session.");
    expect(body).toContainElement(action);
    expect(message).toContainElement(action);
    expect(within(empty as HTMLElement).getByRole("link", { name: "new session" })).toBe(action);
    expect(action).toHaveAttribute("href", "/?project=My%20Project");
    expect(action).toHaveClass("text-primary", "hover:underline");
    expect(action).not.toHaveAttribute("data-slot", "button");
    expect(action.querySelector("svg")).toBeNull();
  });

  it("keeps an iconless header caret visible on a wide coarse-pointer device", () => {
    stubInputScenario(1024, true, false);
    renderSidebar();
    // The "Projects" group header carries no leading icon.
    const header = headerButton("Projects");

    // The parent section label uses the settings-scaled subtitle tier.
    expect(header).toHaveClass("gap-1", "pl-2", "text-sm", "font-normal");
    expect(header).not.toHaveClass("font-medium", "uppercase");

    expect(header.querySelector(".lucide-folder")).toBeNull();

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    // Exactly one chevron (no in-slot swap), with its desktop fade gated on
    // hover capability so a wide touch device keeps the caret visible.
    expect(chevrons).toHaveLength(1);
    const [chevron] = chevrons;
    expect(classOf(chevron)).not.toMatch(/\babsolute\b/);
    expect(chevron).toHaveClass(
      "[@media(hover:hover)]:md:opacity-0",
      "[@media(hover:hover)]:md:group-hover:opacity-100",
      "[@media(hover:hover)]:md:group-focus-visible:opacity-100",
    );
    expect(chevron).not.toHaveClass("md:opacity-0");
  });
});
