// Tests for the Design page (`/design`). The session loader, projects, the
// deck/kit reads, the studio view, and the New design dialog are mocked at
// their seams; the list builder, search, and routing run for real.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as canvasSessionsModule from "@/canvas/canvasSessions";
import type { CanvasSessions } from "@/canvas/canvasSessions";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import type * as conversationsHook from "@/hooks/useConversations";
import { useProjects, type Conversation } from "@/hooks/useConversations";
import {
  fetchDesignSearch,
  fetchDesignIndex,
  fetchKitIndicator,
  reconcileDesignIndex,
} from "@/lib/designDeckApi";
import { DesignPage } from "./DesignPage";

vi.mock("@/canvas/canvasSessions", async (importActual) => ({
  ...(await importActual<typeof canvasSessionsModule>()),
  useCanvasSessions: vi.fn(),
}));
vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useProjects: vi.fn(),
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "me" }));
vi.mock("@/lib/designDeckApi", () => ({
  fetchDesignSearch: vi.fn(),
  fetchDesignIndex: vi.fn(),
  fetchKitIndicator: vi.fn(),
  reconcileDesignIndex: vi.fn(),
}));
vi.mock("./design/DesignStudio", () => ({
  LIVE_QUERY: { staleTime: 0, refetchOnMount: true, refetchOnWindowFocus: true },
  DesignStudio: (props: {
    sessionId: string;
    path: string;
    view: string;
    fresh: boolean;
    onView: (view: string) => void;
    onBack: () => void;
  }) => (
    <div
      data-testid="studio"
      data-session={props.sessionId}
      data-path={props.path}
      data-view={props.view}
      data-fresh={String(props.fresh)}
    >
      <button type="button" onClick={() => props.onView("full")}>
        studio-full
      </button>
      <button type="button" onClick={props.onBack}>
        studio-back
      </button>
    </div>
  ),
}));
vi.mock("./design/NewDesignDialog", () => ({
  NewDesignDialog: (props: {
    open: boolean;
    initialPrompt?: string;
    takenNames: (folder: string, kind: "deck" | "wireframe") => readonly string[];
    onCreated: (sessionId: string, path: string) => void;
  }) =>
    props.open ? (
      <div role="dialog" aria-label="New design">
        <span data-testid="dialog-prompt">{props.initialPrompt ?? ""}</span>
        <span data-testid="dialog-taken">{props.takenNames("/work/a/", "deck").join(",")}</span>
        <span data-testid="dialog-taken-wireframes">
          {props.takenNames("/work/a/", "wireframe").join(",")}
        </span>
        <button type="button" onClick={() => props.onCreated("conv_new", "decks/new.slides.html")}>
          dialog-create
        </button>
      </div>
    ) : null,
}));

const sessionsMock = vi.mocked(useCanvasSessions);
const projectsMock = vi.mocked(useProjects);
const searchMock = vi.mocked(fetchDesignSearch);
const kitMock = vi.mocked(fetchKitIndicator);
const indexMock = vi.mocked(fetchDesignIndex);
const reconcileMock = vi.mocked(reconcileDesignIndex);
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

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function at(): string {
  return screen.getByTestId("location").textContent ?? "";
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
  projectsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
  kitMock.mockResolvedValue({ status: "none" });
  indexMock.mockResolvedValue(null);
  reconcileMock.mockResolvedValue(undefined);
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

    expect(
      await screen.findByText(/No designs yet\. Ask an agent for a slide deck or a wireframe/),
    ).toBeVisible();
    expect(screen.getByText(".slides.html")).toBeInTheDocument();
    expect(screen.getByText(".wireframe.html")).toBeInTheDocument();
  });

  it("does not claim empty while the session list is still loading", () => {
    stubSessions([], { loaded: false, loadingMore: true });
    renderPage();
    expect(screen.getByRole("status", { name: "Loading sessions" })).toBeInTheDocument();
    expect(screen.queryByText(/No designs yet/)).toBeNull();
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
    searchMock.mockResolvedValueOnce({
      status: "ok",
      paths: ["fixed.slides.html"],
      truncated: false,
    });

    renderPage();

    const a = group("a");
    expect(await within(a).findByText(/500 Server Error/, {}, { timeout: 4000 })).toBeVisible();
    fireEvent.click(within(a).getByRole("button", { name: "Retry" }));
    expect(await within(a).findByText("fixed")).toBeInTheDocument();
  });

  it("lists wireframes beside decks with a kind badge on each card", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({
      status: "ok",
      paths: ["decks/q3.slides.html", "wireframes/sign-up.wireframe.html"],
      truncated: false,
    });

    renderPage();

    const deck = await within(group("a")).findByRole("link", { name: /q3/ });
    const wireframe = within(group("a")).getByRole("link", { name: /sign-up/ });
    expect(within(deck).getByText("Slides")).toBeInTheDocument();
    expect(within(wireframe).getByText("Wireframe")).toBeInTheDocument();
    expect(wireframe).toHaveAttribute(
      "href",
      "/design?session=a&file=wireframes%2Fsign-up.wireframe.html",
    );
  });

  it.each([
    [{ status: "ok", name: "Acme" } as const, "Acme"],
    [{ status: "invalid", reason: "kit.json is not valid JSON" } as const, "Kit invalid"],
    [{ status: "system", name: "Brand", kind: "full" } as const, "Brand"],
    [{ status: "system", name: "Brand", kind: "skill" } as const, "Skill-only"],
    [{ status: "invalid", reason: "bad", system: true } as const, "Design system invalid"],
  ])("shows the kit indicator %j", async (kit, label) => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["d.slides.html"], truncated: false });
    kitMock.mockResolvedValue(kit);

    renderPage();

    expect(await within(group("a")).findByText(label)).toBeInTheDocument();
    expect(kitMock).toHaveBeenCalledWith("a");
  });

  it("titles a design-system badge with its name and kind", async () => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["d.slides.html"], truncated: false });
    kitMock.mockResolvedValue({ status: "system", name: "Brand", kind: "full" });

    renderPage();

    const badge = await within(group("a")).findByTitle("Design system: Brand (full)");
    expect(badge).toHaveTextContent("BrandFull");
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

    await screen.findByText(/No designs yet/);
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

    expect(await within(group("a")).findByText(/Search stopped early/)).toHaveTextContent(
      "more decks may exist",
    );
    expect(within(group("a")).getByText("d")).toBeInTheDocument();
  });
});

describe("DesignPage landing", () => {
  beforeEach(() => {
    stubSessions([row("a", 2, { title: "Pitch session" }), row("b", 1)]);
    searchMock.mockImplementation((id) =>
      Promise.resolve({
        status: "ok",
        paths:
          id === "a"
            ? ["decks/pitch.slides.html", "wireframes/flow.wireframe.html"]
            : ["roadmap.slides.html"],
        truncated: false,
      }),
    );
  });

  it("shows the header with its subtitle and New design", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Design", level: 1 })).toBeVisible();
    expect(screen.getByText("Slides your agents made, on brand")).toBeVisible();
    expect(screen.getByRole("button", { name: "New design" })).toBeEnabled();
    expect(await within(group("a")).findByRole("link", { name: /pitch/ })).toBeVisible();
  });

  it("filters cards by deck name, workspace, or session title", async () => {
    renderPage();
    await within(group("b")).findByRole("link", { name: /roadmap/ });
    const search = screen.getByRole("textbox", { name: "Search designs" });

    fireEvent.change(search, { target: { value: "ROAD" } });
    expect(screen.queryByRole("region", { name: "a" })).toBeNull();
    expect(within(group("b")).getByRole("link", { name: /roadmap/ })).toBeVisible();

    fireEvent.change(search, { target: { value: "pitch session" } });
    expect(within(group("a")).getByRole("link", { name: /pitch/ })).toBeVisible();
    expect(screen.queryByRole("region", { name: "b" })).toBeNull();

    fireEvent.change(search, { target: { value: "nothing like this" } });
    expect(screen.getByText("No designs match")).toBeVisible();
  });

  it("opens New design from the button and from a chip with the prompt", async () => {
    renderPage();
    await within(group("a")).findByRole("link", { name: /pitch/ });

    fireEvent.click(screen.getByRole("button", { name: "Weekly status update" }));
    expect(screen.getByTestId("dialog-prompt")).toHaveTextContent("Weekly status update");
    expect(screen.getByTestId("dialog-taken")).toHaveTextContent("pitch");
    expect(screen.getByTestId("dialog-taken-wireframes")).toHaveTextContent(/^flow$/);
  });

  it("shows chips in the empty state", async () => {
    searchMock.mockResolvedValue({ status: "ok", paths: [], truncated: false });
    renderPage();
    await screen.findByText(/No designs yet/);
    fireEvent.click(screen.getByRole("button", { name: "Product launch" }));
    expect(screen.getByTestId("dialog-prompt")).toHaveTextContent("Product launch");
  });

  it("goes to the studio for a created design", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New design" }));
    fireEvent.click(screen.getByRole("button", { name: "dialog-create" }));

    expect(at()).toBe("/design?session=conv_new&file=decks%2Fnew.slides.html");
    expect(screen.getByTestId("studio")).toHaveAttribute("data-fresh", "true");
    expect(refreshMock).toHaveBeenCalled();
  });

  it("opens the studio for an existing deck card", async () => {
    renderPage();
    fireEvent.click(await within(group("a")).findByRole("link", { name: /pitch/ }));

    expect(at()).toBe("/design?session=a&file=decks%2Fpitch.slides.html");
    const studio = screen.getByTestId("studio");
    expect(studio).toHaveAttribute("data-session", "a");
    expect(studio).toHaveAttribute("data-path", "decks/pitch.slides.html");
    expect(studio).toHaveAttribute("data-view", "preview");
    expect(studio).toHaveAttribute("data-fresh", "false");
  });
});

describe("DesignPage studio routing", () => {
  beforeEach(() => {
    stubSessions([row("a", 1)]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["pitch.slides.html"], truncated: false });
  });

  it("opens a linked studio with its view", () => {
    renderPage("/design?session=a&file=pitch.slides.html&view=full");
    expect(screen.getByTestId("studio")).toHaveAttribute("data-view", "full");
    expect(screen.queryByRole("heading", { name: "Design", level: 1 })).toBeNull();
  });

  it("keeps the view in the URL", () => {
    renderPage("/design?session=a&file=pitch.slides.html");
    fireEvent.click(screen.getByRole("button", { name: "studio-full" }));
    expect(at()).toBe("/design?session=a&file=pitch.slides.html&view=full");
    expect(screen.getByTestId("studio")).toHaveAttribute("data-view", "full");
  });

  it("returns to the landing from a card, even after changing the view", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("link", { name: /pitch/ }));
    fireEvent.click(screen.getByRole("button", { name: "studio-full" }));
    fireEvent.click(screen.getByRole("button", { name: "studio-back" }));

    expect(at()).toBe("/design");
    expect(await screen.findByRole("region", { name: "a" })).toBeInTheDocument();
  });

  it("returns to the landing from a linked studio", async () => {
    renderPage("/design?session=a&file=pitch.slides.html");
    fireEvent.click(screen.getByRole("button", { name: "studio-back" }));
    expect(at()).toBe("/design");
    expect(await screen.findByRole("region", { name: "a" })).toBeInTheDocument();
  });
});

describe("DesignPage server index", () => {
  const indexed = (sessionId: string, path: string, workspace: string) => ({
    session_id: sessionId,
    path,
    kind: "deck" as const,
    updated_at: 9,
    session_title: `Indexed ${sessionId}`,
    workspace,
  });

  it("scans every recent session and never reconciles without the index", async () => {
    stubSessions([row("a", 2), row("asleep", 1, { runner_online: false })]);
    searchMock.mockResolvedValue({ status: "ok", paths: ["q3.slides.html"], truncated: false });

    renderPage();

    expect(await within(group("asleep")).findByText("q3")).toBeInTheDocument();
    expect(searchMock).toHaveBeenCalledWith("a");
    expect(searchMock).toHaveBeenCalledWith("asleep");
    expect(reconcileMock).not.toHaveBeenCalled();
  });

  it("scans only live sessions, reconciles them, and lists offline ones from the index", async () => {
    stubSessions([row("a", 2), row("asleep", 1, { runner_online: false })]);
    indexMock.mockResolvedValue([
      indexed("asleep", "decks/old.slides.html", "/work/asleep"),
      indexed("a", "decks/stale.slides.html", "/work/a"),
      indexed("ancient", "pitch.slides.html", "/work/ancient"),
      { ...indexed("ancient", "app.wireframe.html", "/work/ancient"), kind: "wireframe" as const },
    ]);
    searchMock.mockResolvedValue({
      status: "ok",
      paths: ["decks/q3.slides.html", "w/flow.wireframe.html", "node_modules/x/y.slides.html"],
      truncated: false,
    });

    renderPage();

    expect(await within(group("a")).findByText("q3")).toBeInTheDocument();
    expect(within(group("a")).queryByText("stale")).toBeNull();
    expect(searchMock).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(reconcileMock).toHaveBeenCalledWith("a", [
        "decks/q3.slides.html",
        "w/flow.wireframe.html",
      ]),
    );

    const asleep = group("asleep");
    expect(within(asleep).getByText(/Unavailable/)).toBeInTheDocument();
    expect(within(asleep).getByRole("link", { name: /old/ })).toHaveAttribute(
      "href",
      expect.stringContaining("session=asleep"),
    );
    expect(within(group("ancient")).getAllByText("Indexed ancient")).toHaveLength(2);
    expect(within(group("ancient")).getByRole("link", { name: /app/ })).toHaveTextContent(
      "Wireframe",
    );
    expect(kitMock).toHaveBeenCalledTimes(1);
  });
});
