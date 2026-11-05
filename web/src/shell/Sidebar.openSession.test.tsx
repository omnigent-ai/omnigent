// The open session always has a sidebar row: opening one from search reveals it
// even when it is archived or older than the loaded list pages.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import * as identity from "@/lib/identity";
import { sidebarConfig, type SidebarConfig } from "@/lib/sidebarConfig";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";

const mocks = vi.hoisted(() => ({
  archive: { mutate: vi.fn() },
  projects: [] as { id: string; name: string }[],
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useArchiveConversation: () => mocks.archive,
    useProjects: () => ({ data: mocks.projects }),
  };
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const LOADED = [
  conversation("conv_recent", { title: "Recent work", updated_at: 300 }),
  conversation("conv_middle", { title: "Middle work", updated_at: 200 }),
];

/**
 * Render the sidebar on `/c/<openId>` with the given loaded list, as if the
 * session had been opened from search: its row sits only in the search cache.
 */
function renderOpenSession(
  openId: string,
  loaded: Conversation[],
  searchRows: Conversation[],
  config: SidebarConfig = sidebarConfig,
) {
  vi.mocked(useConversations).mockImplementation(() => conversationPage(loaded));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A top-level session: the root walk resolves to the open session itself.
  queryClient.setQueryData(["session", openId], { id: openId, parentSessionId: null });
  queryClient.setQueryData(["conversations", "search", "query"], {
    pages: [{ data: searchRows, first_id: null, last_id: null, has_more: false }],
    pageParams: [undefined],
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SidebarDataProvider config={config}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[`/c/${openId}`]}>
            <Routes>
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

const scrollIntoView = vi.fn();

/** The rows the sidebar scrolled into view, by their link text. */
function scrolledRowTexts() {
  return scrollIntoView.mock.contexts.map((row) => (row as HTMLElement).textContent);
}

/** Wait for the open session's row, and check it is the one scrolled into view. */
async function findOpenRow(title: RegExp) {
  const link = await screen.findByRole("link", { name: title });
  await waitFor(() => expect(scrolledRowTexts().some((text) => title.test(text ?? ""))).toBe(true));
  return link;
}

function sessionsSection() {
  return screen.getByText("Sessions", { selector: "h2, button, span" }).closest("section")!;
}

beforeEach(() => {
  mocks.archive.mutate.mockReset();
  mocks.projects = [];
  scrollIntoView.mockReset();
  vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(scrollIntoView);
  // Search rows carry their owner; the viewer owns the "me" rows.
  vi.spyOn(identity, "getCurrentUserId").mockReturnValue("me");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  localStorage.clear();
});

describe("Sidebar open session reveal", () => {
  it("lists an archived open session on the default view with its Unarchive button", async () => {
    const archived = conversation("conv_archived", {
      title: "Archived work",
      archived: true,
      owner: "me",
      updated_at: 250,
    });
    renderOpenSession("conv_archived", LOADED, [archived]);

    await findOpenRow(/Archived work/);
    // Sorted into place among the live rows.
    const titles = within(sessionsSection())
      .getAllByRole("link")
      .map((link) => link.textContent);
    const position = (title: string) => titles.findIndex((t) => t?.includes(title));
    expect(position("Recent work")).toBeLessThan(position("Archived work"));
    expect(position("Archived work")).toBeLessThan(position("Middle work"));

    const unarchive = screen.getByRole("button", { name: "Unarchive conversation" });
    fireEvent.click(unarchive);
    expect(mocks.archive.mutate).toHaveBeenCalledWith({ id: "conv_archived", archived: false });
  });

  it("lists an archived filed session under Sessions, not its project folder", async () => {
    mocks.projects = [{ id: "proj_1", name: "Customer X" }];
    const archived = conversation("conv_archived", {
      title: "Archived filed work",
      archived: true,
      owner: "me",
      project_id: "proj_1",
    });
    renderOpenSession("conv_archived", LOADED, [archived]);

    const row = await findOpenRow(/Archived filed work/);
    expect(sessionsSection()).toContainElement(row);
    expect(screen.getByRole("button", { name: /^Customer X/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("does not list other archived sessions on the default view", async () => {
    const other = conversation("conv_other", { title: "Other archived", archived: true });
    renderOpenSession("conv_recent", [...LOADED, other], []);

    expect(await screen.findByRole("link", { name: /Recent work/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Other archived/ })).toBeNull();
  });

  it("reveals a live session older than the loaded pages", async () => {
    const older = conversation("conv_older", { title: "Older work", owner: "me", updated_at: 50 });
    renderOpenSession("conv_older", LOADED, [older]);

    const row = await findOpenRow(/Older work/);
    expect(sessionsSection()).toContainElement(row);
    expect(screen.queryByRole("button", { name: "Unarchive conversation" })).toBeNull();
  });

  it("opens the folder of an older session filed only by project id", async () => {
    mocks.projects = [{ id: "proj_1", name: "Customer X" }];
    const older = conversation("conv_older", {
      title: "Older filed work",
      owner: "me",
      project_id: "proj_1",
    });
    renderOpenSession("conv_older", LOADED, [older]);

    const row = await findOpenRow(/Older filed work/);
    expect(screen.getByRole("button", { name: /^Customer X/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Customer X").closest("section")).toContainElement(row);
  });

  it("extends a capped list display to the open session", async () => {
    const many = Array.from({ length: 6 }, (_, index) =>
      conversation(`conv_${index}`, { title: `Session ${index}`, updated_at: 100 - index }),
    );
    renderOpenSession("conv_5", many, [], { ...sidebarConfig, displayPageSize: 2 });

    // Two rows per page would otherwise leave the sixth hidden.
    await findOpenRow(/Session 5/);
  });
});
