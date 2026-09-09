// Tests for the Canvas page (`/canvas`). React Flow is stubbed to a plain list
// that exposes the props the page drives (nodes, drag-stop, double-click), and
// the session loader and project hook are mocked at their seams; the layout,
// storage, and card modules run for real.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import * as conversationsHook from "@/hooks/useConversations";
import * as canvasSessions from "@/canvas/canvasSessions";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { canvasLayoutStorageKey, readCanvasLayout } from "@/canvas/canvasStorage";
import { CanvasPage } from "./CanvasPage";

const { flowProps, flowFitView, flowSetViewport, flowApi } = vi.hoisted(() => {
  const fitViewMock = vi.fn();
  const setViewportMock = vi.fn(async () => true);
  return {
    flowProps: { current: null as Record<string, unknown> | null },
    flowFitView: fitViewMock,
    flowSetViewport: setViewportMock,
    flowApi: {
      fitView: fitViewMock,
      getViewport: vi.fn(() => ({ x: 0, y: 0, zoom: 1 })),
      setViewport: setViewportMock,
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
    },
  };
});

vi.mock("@xyflow/react", () => ({
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: Record<string, unknown>) => {
    flowProps.current = props;
    const nodes = props.nodes as { id: string; position: { x: number; y: number } }[];
    return (
      <div data-testid="react-flow">
        {nodes.map((node) => (
          <button
            key={node.id}
            type="button"
            data-testid={`flow-node-${node.id}`}
            data-x={node.position?.x}
            data-y={node.position?.y}
            onDoubleClick={() =>
              (props.onNodeDoubleClick as (event: MouseEvent, value: unknown) => void)(
                new MouseEvent("dblclick"),
                node,
              )
            }
          >
            {node.id}
          </button>
        ))}
        {props.children as ReactNode}
      </div>
    );
  },
  Background: () => null,
  useReactFlow: () => ({ ...flowApi }),
  applyNodeChanges: (_changes: unknown, nodes: unknown) => nodes,
}));

vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useProjects: vi.fn(),
}));
vi.mock("@/canvas/canvasSessions", () => ({ useCanvasSessions: vi.fn() }));
vi.mock("@/hooks/useGithub", () => ({
  fetchGithubInfo: vi.fn(async () => ({ object: "session.github.info", available: false })),
}));
vi.mock("@/shell/NewProjectButton", () => ({
  NewProjectButton: ({ onCreated }: { onCreated: (name: string) => void }) => (
    <button type="button" onClick={() => onCreated("Release")}>
      New project
    </button>
  ),
}));

function conversation(id: string, updatedAt: number, overrides: Partial<Conversation> = {}) {
  return {
    id,
    object: "conversation",
    title: `Title ${id}`,
    created_at: 1,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    status: "idle",
    workspace: `/workspace/${id}`,
    ...overrides,
  } as Conversation;
}

function sessionsStub(
  rows: Conversation[],
  overrides: Partial<canvasSessions.CanvasSessions> = {},
): canvasSessions.CanvasSessions {
  return {
    sessions: rows,
    loaded: true,
    loadingMore: false,
    complete: true,
    networkConfirmed: true,
    error: null,
    refresh: vi.fn(async () => undefined),
    ...overrides,
  };
}

function projectsStub(projects: ProjectSummary[] | undefined) {
  return { data: projects } as unknown as ReturnType<typeof conversationsHook.useProjects>;
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function pageTree(initialEntry = "/canvas") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route
              path="/canvas"
              element={
                <>
                  <CanvasPage />
                  <LocationProbe />
                </>
              }
            />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function renderPage(initialEntry = "/canvas") {
  return render(pageTree(initialEntry));
}

const PROJECTS: ProjectSummary[] = [
  { id: "proj_a", name: "Alpha", icon: "🔥" },
  { id: null, name: "Legacy" },
];

beforeEach(() => {
  window.localStorage.clear();
  flowProps.current = null;
  flowFitView.mockClear();
  flowSetViewport.mockClear();
  vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(sessionsStub([]));
  vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub([]));
});

afterEach(() => {
  cleanup();
});

describe("CanvasPage", () => {
  it("renders loaded sessions as cards while later pages are still arriving", async () => {
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 3), conversation("conv_2", 2)], {
        loadingMore: true,
        complete: false,
        networkConfirmed: false,
      }),
    );
    renderPage();

    expect(screen.getByRole("heading", { name: "Canvas" })).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-conv_1")).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-conv_2")).toBeInTheDocument();
    expect(screen.getByText("2 sessions")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading sessions" })).toBeInTheDocument();
    // Whatever has arrived is kept in view; later pages refit as they land.
    await waitFor(() => expect(flowFitView).toHaveBeenCalled());
    expect(flowSetViewport).not.toHaveBeenCalled();
  });

  it("fits a completed canvas of any size and keeps the count quiet", async () => {
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub(Array.from({ length: 600 }, (_, index) => conversation(`conv_${index}`, index))),
    );
    renderPage();
    expect(screen.getByText("600 sessions")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading sessions" })).toBeNull();
    await waitFor(() => expect(flowFitView).toHaveBeenCalled());
    expect(flowSetViewport).not.toHaveBeenCalled();
    // Every card carries its size so the fit covers cards not yet rendered.
    const nodes = flowProps.current?.nodes as { initialWidth: number; initialHeight: number }[];
    expect(nodes).toHaveLength(600);
    expect(nodes[0]).toMatchObject({ initialWidth: 280, initialHeight: 132 });
    // Cards snap to the layout lattice while dragging.
    expect(flowProps.current).toMatchObject({ snapToGrid: true, snapGrid: [32, 32] });
  });

  it("refits on a tab change or layout reset, but not on status-only updates", async () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    const rows = [
      conversation("conv_main", 2),
      conversation("conv_alpha", 1, { project_id: "proj_a" }),
    ];
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(sessionsStub(rows));
    const { rerender } = renderPage();
    await waitFor(() => expect(flowFitView).toHaveBeenCalledTimes(1));

    // Same cards, new status: the view stays put.
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([{ ...rows[0], status: "running" }, rows[1]]),
    );
    rerender(pageTree());
    expect(screen.getByTestId("flow-node-conv_main")).toBeInTheDocument();
    expect(flowFitView).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("tab", { name: "Alpha" }));
    await waitFor(() => expect(flowFitView).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "Reset layout" }));
    await waitFor(() => expect(flowFitView).toHaveBeenCalledTimes(3));
  });

  it("groups sessions into Main and project canvases and switches between them", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([
        conversation("conv_loose", 3),
        conversation("conv_alpha", 2, { project_id: "proj_a" }),
        conversation("conv_legacy", 1, { labels: { [PROJECT_LABEL_KEY]: "Legacy" } }),
      ]),
    );
    renderPage();

    const tabs = screen.getByRole("tablist");
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual(["Main", "🔥Alpha", "Legacy"]);
    expect(screen.getByTestId("flow-node-conv_loose")).toBeInTheDocument();
    expect(screen.queryByTestId("flow-node-conv_alpha")).toBeNull();

    fireEvent.click(within(tabs).getByRole("tab", { name: "Alpha" }));
    expect(within(tabs).getByRole("tab", { name: "Alpha" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("flow-node-conv_alpha")).toBeInTheDocument();
    expect(screen.queryByTestId("flow-node-conv_loose")).toBeNull();
    expect(screen.getByText("1 session")).toBeInTheDocument();

    fireEvent.click(within(tabs).getByRole("tab", { name: "Legacy" }));
    expect(screen.getByTestId("flow-node-conv_legacy")).toBeInTheDocument();
  });

  it("restores the selected canvas from the URL and writes tab changes back to it", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_alpha", 2, { project_id: "proj_a" })]),
    );
    renderPage("/canvas?canvas=proj_a");

    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("flow-node-conv_alpha")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Legacy" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/canvas?canvas=name%3ALegacy");

    fireEvent.click(screen.getByRole("tab", { name: "Main" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/canvas$/);
  });

  it("reopens the last selected canvas on a visit without a URL parameter", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    const { unmount } = renderPage();
    fireEvent.click(screen.getByRole("tab", { name: "Alpha" }));
    unmount();

    const revisit = renderPage();
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("location")).toHaveTextContent("/canvas?canvas=proj_a");
    revisit.unmount();

    // An explicit URL still wins over the remembered canvas.
    renderPage("/canvas?canvas=name%3ALegacy");
    expect(screen.getByRole("tab", { name: "Legacy" })).toHaveAttribute("aria-selected", "true");
    cleanup();
    renderPage();
    expect(screen.getByRole("tab", { name: "Legacy" })).toHaveAttribute("aria-selected", "true");
  });

  it("falls back to Main when the URL names a canvas that no longer exists", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    renderPage("/canvas?canvas=proj_gone");

    expect(screen.getByRole("tab", { name: "Main" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/canvas$/);
    // The stale choice is forgotten too, so the next visit does not retry it.
    cleanup();
    renderPage();
    expect(screen.getByRole("tab", { name: "Main" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows explicit empty states per canvas", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_alpha", 2, { project_id: "proj_a" })]),
    );
    renderPage();
    expect(screen.getByText("No sessions outside projects")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Legacy" }));
    expect(screen.getByText("No sessions in Legacy")).toBeInTheDocument();
  });

  it("opens a session on double-click and starts new sessions in the active project", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 1, { project_id: "proj_a" })]),
    );
    const { unmount } = renderPage();

    fireEvent.click(screen.getByTestId("canvas-new-session"));
    expect(screen.getByTestId("location")).toHaveTextContent("/");
    unmount();

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: "Alpha" }));
    fireEvent.click(screen.getByTestId("canvas-new-session"));
    expect(screen.getByTestId("location")).toHaveTextContent("/?project=Alpha");
    cleanup();

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: "Alpha" }));
    fireEvent.doubleClick(screen.getByTestId("flow-node-conv_1"));
    expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_1");
  });

  it("saves every card's spot once complete, so a moved card leaves the others in place", async () => {
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 2), conversation("conv_2", 1)]),
    );
    const { unmount } = renderPage();
    // Grid slots are saved as soon as the full list is known, not only after a drag.
    expect(readCanvasLayout(null)).toEqual({
      positions: { conv_1: { x: 0, y: 0 }, conv_2: { x: 320, y: 0 } },
    });
    const dragStop = flowProps.current?.onNodeDragStop as (
      event: MouseEvent,
      node: { id: string; position: { x: number; y: number } },
    ) => void;
    act(() => {
      dragStop(new MouseEvent("mouseup"), { id: "conv_1", position: { x: 960.4, y: 480.6 } });
    });
    expect(readCanvasLayout(null).positions).toEqual({
      conv_2: { x: 320, y: 0 },
      conv_1: { x: 960, y: 481 },
    });
    unmount();

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("flow-node-conv_1")).toHaveAttribute("data-x", "960"),
    );
    expect(screen.getByTestId("flow-node-conv_1")).toHaveAttribute("data-y", "481");
    // The unmoved card keeps its slot instead of sliding into the vacated one.
    expect(screen.getByTestId("flow-node-conv_2")).toHaveAttribute("data-x", "320");
    // The restored layout is fitted like any other first paint.
    await waitFor(() => expect(flowFitView).toHaveBeenCalled());
  });

  it("does not save grid slots while the list is still loading", () => {
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 2)], {
        loadingMore: true,
        complete: false,
        networkConfirmed: false,
      }),
    );
    renderPage();
    expect(readCanvasLayout(null)).toEqual({ positions: {} });
  });

  it("does not prune positions while the complete list is only cached", () => {
    window.localStorage.setItem(
      canvasLayoutStorageKey(null),
      JSON.stringify({ version: 1, positions: { conv_cached_out: [11, 12], conv_1: [7, 7] } }),
    );
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 1)], { networkConfirmed: false }),
    );
    renderPage();
    expect(readCanvasLayout(null).positions).toEqual({
      conv_cached_out: { x: 11, y: 12 },
      conv_1: { x: 7, y: 7 },
    });
  });

  it("resets only the active canvas's positions to grid slots", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub(PROJECTS));
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([
        conversation("conv_main", 2),
        conversation("conv_alpha", 1, { project_id: "proj_a" }),
      ]),
    );
    window.localStorage.setItem(
      canvasLayoutStorageKey(null),
      JSON.stringify({
        version: 1,
        positions: { conv_main: [900, 900], conv_alpha: [50, 50] },
      }),
    );
    renderPage();
    expect(screen.getByTestId("flow-node-conv_main")).toHaveAttribute("data-x", "900");

    fireEvent.click(screen.getByRole("button", { name: "Reset layout" }));

    expect(screen.getByTestId("flow-node-conv_main")).toHaveAttribute("data-x", "0");
    expect(readCanvasLayout(null).positions).toEqual({
      conv_alpha: { x: 50, y: 50 },
      conv_main: { x: 0, y: 0 },
    });
  });

  it("forgets saved spots of deleted sessions once the list is complete", () => {
    window.localStorage.setItem(
      canvasLayoutStorageKey(null),
      JSON.stringify({ version: 1, positions: { conv_gone: [1, 1], conv_1: [7, 7] } }),
    );
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 1)]),
    );
    renderPage();
    expect(readCanvasLayout(null).positions).toEqual({ conv_1: { x: 7, y: 7 } });
  });

  it("selects a project created from the tab strip once the list includes it", () => {
    vi.mocked(conversationsHook.useProjects).mockReturnValue(projectsStub([]));
    const { rerender } = renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(screen.getByRole("tab", { name: "Main" })).toHaveAttribute("aria-selected", "true");

    vi.mocked(conversationsHook.useProjects).mockReturnValue(
      projectsStub([{ id: "proj_release", name: "Release" }]),
    );
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/canvas"]}>
            <Routes>
              <Route path="/canvas" element={<CanvasPage />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("tab", { name: "Release" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("No sessions in Release")).toBeInTheDocument();
  });

  it("shows an initial error with retry, then a quiet banner once cards exist", () => {
    const refresh = vi.fn(async () => undefined);
    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([], {
        loaded: false,
        complete: false,
        networkConfirmed: false,
        error: "boom",
        refresh,
      }),
    );
    const { unmount } = renderPage();
    expect(screen.getByRole("alert")).toHaveTextContent("Canvas could not load");
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refresh).toHaveBeenCalled();
    unmount();

    vi.mocked(canvasSessions.useCanvasSessions).mockReturnValue(
      sessionsStub([conversation("conv_1", 1)], { error: "offline" }),
    );
    renderPage();
    expect(screen.getByTestId("flow-node-conv_1")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Refresh failed: offline");
  });
});
