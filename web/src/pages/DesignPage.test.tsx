// Tests for the Design page (`/design`). The session loader, projects, the
// deck/kit reads, and the deck viewer are mocked at their seams; the list
// builder and routing run for real.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as canvasSessionsModule from "@/canvas/canvasSessions";
import type { CanvasSessions } from "@/canvas/canvasSessions";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import type * as conversationsHook from "@/hooks/useConversations";
import { useProjects, type Conversation } from "@/hooks/useConversations";
import { fetchFileContent, type FileContentResponse } from "@/hooks/useFileContent";
import { fetchDeckSearch, fetchKitIndicator } from "@/lib/designDeckApi";
import { DesignPage } from "./DesignPage";

const { mobileRef } = vi.hoisted(() => ({ mobileRef: { current: false } }));

vi.mock("@/canvas/canvasSessions", async (importActual) => ({
  ...(await importActual<typeof canvasSessionsModule>()),
  useCanvasSessions: vi.fn(),
}));
vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useProjects: vi.fn(),
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "me" }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => mobileRef.current }));
vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/lib/designDeckApi", () => ({ fetchDeckSearch: vi.fn(), fetchKitIndicator: vi.fn() }));
vi.mock("@/shell/SlidesViewer", () => ({
  SlidesViewer: ({ content, conversationId }: { content: string; conversationId?: string }) => (
    <div data-testid="slides-viewer" data-session={conversationId}>
      {content}
    </div>
  ),
}));

const sessionsMock = vi.mocked(useCanvasSessions);
const projectsMock = vi.mocked(useProjects);
const searchMock = vi.mocked(fetchDeckSearch);
const kitMock = vi.mocked(fetchKitIndicator);
const contentMock = vi.mocked(fetchFileContent);
const refreshMock = vi.fn(async () => {});

function row(id: string, updatedAt: number, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: `Title ${id}`,
    created_at: 1,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    workspace: `/work/${id}`,
    ...overrides,
  };
}

function stubSessions(rows: Conversation[], overrides: Partial<CanvasSessions> = {}) {
  sessionsMock.mockReturnValue({
    sessions: rows,
    loaded: true,
    loadingMore: false,
    complete: true,
    networkConfirmed: true,
    error: null,
    refresh: refreshMock,
    ...overrides,
  });
}

function deckFile(content: string): FileContentResponse {
  return {
    object: "session.environment.filesystem.file_content",
    path: "deck.slides.html",
    content_type: "text/html",
    encoding: "utf-8",
    content,
    bytes: content.length,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderPage(path = "/design") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/design" element={<DesignPage />} />
          <Route path="/c/:id" element={<div>session page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function group(label: string): HTMLElement {
  return screen.getByRole("region", { name: label });
}

beforeEach(() => {
  mobileRef.current = false;
  projectsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
  kitMock.mockResolvedValue({ status: "none" });
  contentMock.mockResolvedValue(deckFile("<section>Slide</section>"));
  refreshMock.mockClear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DesignPage list", () => {
  it("shows a skeleton per group and renders finished groups without waiting", async () => {
    stubSessions([row("a", 2), row("b", 1)]);
    searchMock.mockImplementation((id) =>
      id === "a"
        ? Promise.resolve({ status: "ok", paths: ["decks/q3.slides.html"], truncated: false })
        : new Promise(() => {}),
    );

    renderPage();

    expect(await within(group("a")).findByText("q3")).toBeInTheDocument();
    expect(within(group("b")).getByRole("status", { name: "Searching b" })).toBeInTheDocument();
  });

  it("lists each deck with its name, workspace path, and session title", async () => {
    stubSessions([
      row("a", 3, { title: "Quarterly review", project_id: "p1" }),
      row("older", 1, { workspace: "/work/a" }),
    ]);
    projectsMock.mockReturnValue({
      data: [{ id: "p1", name: "Launch" }],
    } as unknown as ReturnType<typeof useProjects>);
    searchMock.mockResolvedValue({
      status: "ok",
      paths: ["decks/q3.slides.html", ".worktrees/x/decks/q3.slides.html"],
      truncated: false,
    });

    renderPage();

    const launch = await screen.findByRole("region", { name: "Launch" });
    const link = await within(launch).findByRole("link", { name: /q3/ });
    expect(link).toHaveTextContent("decks/q3.slides.html");
    expect(link).toHaveTextContent("Quarterly review");
    expect(within(launch).getAllByRole("link", { name: /q3/ })).toHaveLength(1);
    expect(searchMock).toHaveBeenCalledTimes(1);
    expect(searchMock).toHaveBeenCalledWith("a");
  });

  it("shows the empty state once nothing has decks", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: [], truncated: false });

    renderPage();

    expect(await screen.findByText(/No decks yet\. Ask an agent for a slide deck/)).toBeVisible();
    expect(screen.getByText(".slides.html")).toBeInTheDocument();
  });

  it("does not claim empty while the session list is still loading", () => {
    stubSessions([], { loaded: false, loadingMore: true });
    renderPage();
    expect(screen.getByRole("status", { name: "Loading sessions" })).toBeInTheDocument();
    expect(screen.queryByText(/No decks yet/)).toBeNull();
  });

  it("keeps an unavailable workspace visible with a link to its session", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "unavailable" });

    renderPage();

    const a = group("a");
    expect(
      await within(a).findByText("Unavailable: open the session to start its runner"),
    ).toBeVisible();
    expect(within(a).getByRole("link", { name: "Open session" })).toHaveAttribute("href", "/c/a");
  });

  it("shows a search error with a retry that searches again", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockRejectedValueOnce(new Error("500 Server Error"));
    searchMock.mockResolvedValueOnce({ status: "ok", paths: ["fixed.slides.html"], truncated: false });

    renderPage();

    const a = group("a");
    expect(await within(a).findByText(/500 Server Error/, {}, { timeout: 4000 })).toBeVisible();
    fireEvent.click(within(a).getByRole("button", { name: "Retry" }));
    expect(await within(a).findByText("fixed")).toBeInTheDocument();
  });

  it.each([
    [{ status: "ok", name: "Acme" } as const, "Acme"],
    [{ status: "invalid", reason: "kit.json is not valid JSON" } as const, "Kit invalid"],
  ])("shows the kit indicator %j", async (kit, label) => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["d.slides.html"], truncated: false });
    kitMock.mockResolvedValue(kit);

    renderPage();

    expect(await within(group("a")).findByText(label)).toBeInTheDocument();
    expect(kitMock).toHaveBeenCalledWith("a");
  });

  it("links No kit to the sample kit instructions", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["d.slides.html"], truncated: false });

    renderPage();

    const noKit = await within(group("a")).findByRole("link", { name: "No kit" });
    expect(noKit.getAttribute("href")).toMatch(/examples\/design-kits\/sample\/README\.md$/);
  });

  it("does not read the kit for a workspace without decks", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: [], truncated: false });

    renderPage();

    await screen.findByText(/No decks yet/);
    expect(kitMock).not.toHaveBeenCalled();
  });

  it("refreshes the session list and every search", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["d.slides.html"], truncated: false });

    renderPage();
    await within(group("a")).findByText("d");
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(refreshMock).toHaveBeenCalled();
    await waitFor(() => expect(searchMock).toHaveBeenCalledTimes(2));
  });

  it("tells the user when a workspace search hit its result cap", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({
      status: "ok",
      paths: ["d.slides.html"],
      truncated: true,
    });

    renderPage();

    expect(
      await within(group("a")).findByText(/Search stopped early/),
    ).toHaveTextContent("more decks may exist");
    expect(within(group("a")).getByText("d")).toBeInTheDocument();
  });
});

describe("DesignPage selection", () => {
  beforeEach(() => {
    stubSessions([row("a", 1, { title: "Pitch session" })]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["decks/pitch.slides.html"], truncated: false });
  });

  it("shows a hint until a deck is selected", async () => {
    renderPage();
    expect(await screen.findByText("Select a deck to view it here.")).toBeVisible();
    expect(screen.queryByTestId("slides-viewer")).toBeNull();
  });

  it("selects a deck through the URL and renders it with its session", async () => {
    contentMock.mockResolvedValue(deckFile("<section>Pitch</section>"));
    renderPage();

    fireEvent.click(await screen.findByRole("link", { name: /pitch/ }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/design?session=a&file=decks%2Fpitch.slides.html",
    );
    const viewer = await screen.findByTestId("slides-viewer");
    expect(viewer).toHaveTextContent("<section>Pitch</section>");
    expect(viewer).toHaveAttribute("data-session", "a");
    expect(contentMock).toHaveBeenCalledWith("a", "decks/pitch.slides.html");
    expect(screen.getByRole("link", { name: /pitch/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "Open in session" })).toHaveAttribute(
      "href",
      "/c/a?file=decks%2Fpitch.slides.html",
    );
  });

  it("opens a linked deck directly", async () => {
    renderPage("/design?session=a&file=decks%2Fpitch.slides.html");
    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
  });

  it("shows a failed deck read with the Open in session link", async () => {
    contentMock.mockRejectedValue(new Error("403 Forbidden"));
    renderPage("/design?session=a&file=decks%2Fpitch.slides.html");

    expect(await screen.findByText(/403 Forbidden/, {}, { timeout: 4000 })).toBeVisible();
    expect(screen.queryByTestId("slides-viewer")).toBeNull();
    expect(screen.getByRole("link", { name: "Open in session" })).toHaveAttribute(
      "href",
      "/c/a?file=decks%2Fpitch.slides.html",
    );
  });
});

describe("DesignPage on a phone", () => {
  beforeEach(() => {
    mobileRef.current = true;
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["pitch.slides.html"], truncated: false });
  });

  it("shows only the list until a deck is chosen, then only the viewer", async () => {
    renderPage();

    fireEvent.click(await screen.findByRole("link", { name: /pitch/ }));

    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "a" })).toBeNull();
    expect(screen.queryByText("Select a deck to view it here.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back to decks" }));

    expect(screen.getByTestId("location")).toHaveTextContent(/^\/design$/);
    expect(await screen.findByRole("region", { name: "a" })).toBeInTheDocument();
    expect(screen.queryByTestId("slides-viewer")).toBeNull();
  });

  it("goes back to the list from a linked deck too", async () => {
    renderPage("/design?session=a&file=pitch.slides.html");

    fireEvent.click(await screen.findByRole("button", { name: "Back to decks" }));

    expect(screen.getByTestId("location")).toHaveTextContent(/^\/design$/);
    expect(await screen.findByRole("region", { name: "a" })).toBeInTheDocument();
  });
});
