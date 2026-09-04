import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ExtensionContext,
  ExtensionProjectSummary,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";

const { flowProps, fitView, getViewport, setViewport, flowApi } = vi.hoisted(
  () => {
    const fitView = vi.fn();
    const getViewport = vi.fn(() => ({ x: 0, y: 0, zoom: 1 }));
    const setViewport = vi.fn(async () => true);
    return {
      flowProps: { current: null as Record<string, unknown> | null },
      fitView,
      getViewport,
      setViewport,
      flowApi: { fitView, getViewport, setViewport },
    };
  },
);

vi.mock("@xyflow/react", () => ({
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: (props: Record<string, unknown>) => {
    flowProps.current = props;
    const nodes = props.nodes as Array<{ id: string }>;
    return (
      <div data-testid="react-flow">
        {nodes.map((node) => (
          <button
            key={node.id}
            type="button"
            data-testid={`flow-node-${node.id}`}
            onDoubleClick={() =>
              (
                props.onNodeDoubleClick as (
                  event: MouseEvent,
                  value: unknown,
                ) => void
              )(new MouseEvent("dblclick"), node)
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
  Controls: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ControlButton: ({
    children,
    ...props
  }: Record<string, unknown> & { children?: ReactNode }) => (
    <button type="button" {...props}>
      {children}
    </button>
  ),
  useReactFlow: () => flowApi,
  applyNodeChanges: (_changes: unknown, nodes: unknown) => nodes,
}));

import { CanvasApp, SESSION_POLL_INTERVAL_MS } from "./CanvasApp";
import {
  LAYOUT_META_KEY,
  positionBucket,
  positionBucketKey,
  viewportKey,
} from "./canvasStorage";

const sessions: ExtensionSessionSummary[] = [
  {
    id: "conv_1",
    title: "One",
    status: "running",
    unread: false,
    titleProvisional: false,
    gitBranch: null,
    workspace: "/workspace/one",
    projectId: null,
    createdAt: 1,
    updatedAt: 2,
  },
  {
    id: "conv_2",
    title: "Two",
    status: "idle",
    unread: false,
    titleProvisional: false,
    gitBranch: null,
    workspace: "/workspace/two",
    projectId: null,
    createdAt: 1,
    updatedAt: 1,
  },
];

function contextWith(
  items: ExtensionSessionSummary[] = sessions,
  projects: ExtensionProjectSummary[] | null = [],
): {
  context: ExtensionContext;
  openSession: ReturnType<typeof vi.fn>;
  values: Map<string, unknown>;
} {
  const values = new Map<string, unknown>();
  const openSession = vi.fn(async () => undefined);
  // `projects: null` models a host without the projects permissions.
  const context = {
    capabilities: [
      "navigation.openSession",
      "navigation.openNewSession",
      "navigation.openExternal",
      "sessions.listPage",
      "sessions.pullRequest",
      ...(projects ? ["projects.list", "projects.create"] : []),
    ],
    navigation: {
      openSession,
      openNewSession: vi.fn(async () => undefined),
      openExternal: vi.fn(async () => undefined),
    },
    sessions: {
      listAll: vi.fn(async () => items),
      pullRequest: vi.fn(async (sessionId: string) =>
        sessionId === "conv_branch"
          ? {
              number: 7,
              title: "Ship it",
              state: "OPEN",
              url: "https://github.com/a/b/pull/7",
            }
          : null,
      ),
    },
    projects: {
      list: vi.fn(async () => projects ?? []),
      create: vi.fn(async ({ name }: { name: string }) => ({
        id: `proj_${name.toLowerCase()}`,
        name,
        icon: null,
      })),
    },
    storage: {
      user: {
        get: vi.fn(async (key: string) => values.get(key) ?? null),
        set: vi.fn(async (key: string, value: unknown) => {
          values.set(key, structuredClone(value));
        }),
        delete: vi.fn(async (key: string) => {
          values.delete(key);
        }),
      },
    },
  } as unknown as ExtensionContext;
  return { context, openSession, values };
}

beforeEach(() => {
  flowProps.current = null;
  fitView.mockReset();
  getViewport.mockClear();
  setViewport.mockClear();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("CanvasApp", () => {
  it("loads every session into a draggable controlled canvas", async () => {
    const { context } = contextWith();
    render(<CanvasApp context={context} />);

    expect(await screen.findByText("2 sessions")).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-conv_1")).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-conv_2")).toBeInTheDocument();
    expect(flowProps.current?.nodesDraggable).toBe(true);
    expect(flowProps.current?.nodesFocusable).toBe(false);
    expect(flowProps.current?.zoomOnDoubleClick).toBe(false);
    expect(flowProps.current?.onlyRenderVisibleElements).toBe(true);
  });

  it("opens a card once on double-click", async () => {
    const { context, openSession } = contextWith();
    render(<CanvasApp context={context} />);
    const card = await screen.findByTestId("flow-node-conv_1");

    fireEvent.doubleClick(card);
    fireEvent.doubleClick(card);

    expect(openSession).toHaveBeenCalledOnce();
    expect(openSession).toHaveBeenCalledWith("conv_1");
  });

  it("keeps existing card positions when a focus refresh adds a session, and resets on demand", async () => {
    const { context } = contextWith();
    vi.mocked(context.sessions.listAll)
      .mockResolvedValueOnce(sessions)
      .mockResolvedValueOnce([
        ...sessions,
        {
          id: "conv_3",
          title: "Three",
          status: "idle",
          unread: false,
          titleProvisional: false,
          gitBranch: null,
          workspace: "/workspace/three",
          projectId: null,
          createdAt: 3,
          updatedAt: 3,
        },
      ]);
    render(<CanvasApp context={context} />);
    await screen.findByText("2 sessions");
    const before = Object.fromEntries(
      (
        flowProps.current?.nodes as Array<{ id: string; position: unknown }>
      ).map((node) => [node.id, node.position]),
    );

    fireEvent(window, new Event("focus"));

    await screen.findByText("3 sessions");
    const after = Object.fromEntries(
      (
        flowProps.current?.nodes as Array<{ id: string; position: unknown }>
      ).map((node) => [node.id, node.position]),
    );
    expect(after.conv_1).toEqual(before.conv_1);
    expect(after.conv_2).toEqual(before.conv_2);
    expect(after.conv_3).toBeDefined();

    fitView.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Reset layout" }));
    await waitFor(() => expect(fitView).toHaveBeenCalled());
  });

  it("persists a dragged card position in its bucket", async () => {
    const { context, values } = contextWith();
    render(<CanvasApp context={context} />);
    await screen.findByText("2 sessions");
    const node = (
      flowProps.current?.nodes as Array<Record<string, unknown>>
    )[0];

    (
      flowProps.current?.onNodeDragStop as (
        event: MouseEvent,
        node: unknown,
      ) => void
    )(new MouseEvent("mouseup"), { ...node, position: { x: 123.4, y: 456.7 } });

    const key = positionBucketKey(positionBucket(String(node.id)));
    await waitFor(() =>
      expect(values.get(key)).toContainEqual([String(node.id), 123, 457]),
    );
  });

  it("shows explicit empty and initial error states", async () => {
    const empty = contextWith([]);
    const rendered = render(<CanvasApp context={empty.context} />);
    expect(await screen.findByText("No sessions")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(empty.context.navigation.openNewSession).toHaveBeenCalledWith(
      undefined,
    );
    rendered.unmount();

    const failed = contextWith();
    vi.mocked(failed.context.sessions.listAll).mockRejectedValue(
      new Error("offline"),
    );
    render(<CanvasApp context={failed.context} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  });

  it("shows a Main canvas plus one canvas per project and switches between them", async () => {
    const projectSession: ExtensionSessionSummary = {
      ...sessions[1],
      id: "conv_p",
      title: "In project",
      projectId: "proj_a",
    };
    const { context, values } = contextWith(
      [...sessions, projectSession],
      [{ id: "proj_a", name: "Alpha", icon: "🅰️" }],
    );
    render(<CanvasApp context={context} />);

    expect(await screen.findByText("2 sessions")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Main" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByTestId("flow-node-conv_p")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Alpha" }));

    expect(await screen.findByText("1 session")).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-conv_p")).toBeInTheDocument();
    expect(screen.queryByTestId("flow-node-conv_1")).not.toBeInTheDocument();
    await waitFor(() => expect(fitView).toHaveBeenCalled());

    (
      flowProps.current?.onMoveEnd as (
        event: null,
        viewport: { x: number; y: number; zoom: number },
      ) => void
    )(null, { x: 5, y: 6, zoom: 1.5 });
    await waitFor(() =>
      expect(values.get(viewportKey("proj_a"))).toEqual({
        x: 5,
        y: 6,
        zoom: 1.5,
        width: 0,
        height: 0,
      }),
    );
    expect(values.has(viewportKey("main"))).toBe(false);
  });

  it("creates a project from the + tab and opens its empty canvas", async () => {
    const { context } = contextWith();
    render(<CanvasApp context={context} />);
    await screen.findByText("2 sessions");

    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByLabelText("Project name"), {
      target: { value: "  Beta " },
    });
    fireEvent.keyDown(screen.getByLabelText("Project name"), { key: "Enter" });

    expect(context.projects.create).toHaveBeenCalledWith({ name: "Beta" });
    expect(await screen.findByRole("tab", { name: "Beta" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("No sessions in Beta")).toBeInTheDocument();
    expect(screen.getByText("0 sessions")).toBeInTheDocument();
    expect(screen.queryByLabelText("Project name")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(context.navigation.openNewSession).toHaveBeenCalledWith({
      projectId: "proj_beta",
    });
  });

  it("falls back to a single canvas without the projects capability", async () => {
    const { context } = contextWith(
      [...sessions, { ...sessions[1], id: "conv_p", projectId: "proj_a" }],
      null,
    );
    render(<CanvasApp context={context} />);

    expect(await screen.findByText("3 sessions")).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "New project" }),
    ).not.toBeInTheDocument();
    expect(context.projects.list).not.toHaveBeenCalled();
  });

  it("restores a saved viewport only when it was made for this container size", async () => {
    const stale = contextWith();
    stale.values.set(LAYOUT_META_KEY, { version: 1 });
    stale.values.set(viewportKey("main"), {
      x: 5,
      y: 6,
      zoom: 0.8,
      width: 1400,
      height: 800,
    });
    const rendered = render(<CanvasApp context={stale.context} />);
    await screen.findByText("2 sessions");
    await waitFor(() => expect(fitView).toHaveBeenCalled());
    expect(setViewport).not.toHaveBeenCalled();
    rendered.unmount();
    fitView.mockClear();

    // jsdom containers measure 0x0, so a viewport saved for 0x0 matches.
    const matching = contextWith();
    matching.values.set(LAYOUT_META_KEY, { version: 1 });
    matching.values.set(viewportKey("main"), {
      x: 5,
      y: 6,
      zoom: 0.8,
      width: 0,
      height: 0,
    });
    render(<CanvasApp context={matching.context} />);
    await screen.findByText("2 sessions");
    await waitFor(() =>
      expect(setViewport).toHaveBeenCalledWith(
        { x: 5, y: 6, zoom: 0.8 },
        { duration: 0 },
      ),
    );
    expect(fitView).not.toHaveBeenCalled();
  });

  it("refits on container resize until the user pans by hand", async () => {
    const callbacks: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          callbacks.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
    const { context } = contextWith();
    render(<CanvasApp context={context} />);
    await screen.findByText("2 sessions");
    await waitFor(() => expect(fitView).toHaveBeenCalledTimes(1));
    const resize = () => callbacks.forEach((callback) => callback());

    resize(); // the initial observe notification is not a resize
    resize();
    await waitFor(() => expect(fitView).toHaveBeenCalledTimes(2));

    (
      flowProps.current?.onMoveEnd as (
        event: MouseEvent,
        viewport: { x: number; y: number; zoom: number },
      ) => void
    )(new MouseEvent("mouseup"), { x: 10, y: 10, zoom: 1 });
    resize();
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(fitView).toHaveBeenCalledTimes(2);
  });

  it("polls for session changes while the canvas is open", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { context } = contextWith();
      vi.mocked(context.sessions.listAll)
        .mockResolvedValueOnce(sessions)
        .mockResolvedValue([
          { ...sessions[0], title: "One (renamed)", status: "running" },
          sessions[1],
        ]);
      render(<CanvasApp context={context} />);
      await screen.findByText("2 sessions");
      expect(context.sessions.listAll).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(SESSION_POLL_INTERVAL_MS + 50);

      await waitFor(() =>
        expect(context.sessions.listAll).toHaveBeenCalledTimes(2),
      );
      const node = (
        flowProps.current?.nodes as Array<{
          id: string;
          data: { session: { title: string; status: string } };
        }>
      ).find((item) => item.id === "conv_1");
      expect(node?.data.session).toMatchObject({
        title: "One (renamed)",
        status: "running",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("looks up each session's pull request once and hands it to the card", async () => {
    const { context } = contextWith([
      ...sessions,
      {
        ...sessions[1],
        id: "conv_branch",
        title: "Branch",
        gitBranch: "feat/x",
      },
    ]);
    render(<CanvasApp context={context} />);
    await screen.findByText("3 sessions");

    await waitFor(() =>
      expect(context.sessions.pullRequest).toHaveBeenCalledWith("conv_branch"),
    );
    expect(context.sessions.pullRequest).toHaveBeenCalledTimes(3);
    await waitFor(() => {
      const nodes = flowProps.current?.nodes as Array<{
        id: string;
        data: { pullRequest: { number: number } | null };
      }>;
      expect(
        nodes.find((node) => node.id === "conv_branch")?.data.pullRequest,
      ).toMatchObject({
        number: 7,
      });
    });
  });
});
