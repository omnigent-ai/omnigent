import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ExtensionContext,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";

const { flowProps, fitView, getViewport, flowApi } = vi.hoisted(() => {
  const fitView = vi.fn();
  const getViewport = vi.fn(() => ({ x: 0, y: 0, zoom: 1 }));
  return {
    flowProps: { current: null as Record<string, unknown> | null },
    fitView,
    getViewport,
    flowApi: { fitView, getViewport },
  };
});

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
      </div>
    );
  },
  Background: () => null,
  Controls: () => null,
  useReactFlow: () => flowApi,
  applyNodeChanges: (_changes: unknown, nodes: unknown) => nodes,
}));

import { CanvasApp } from "./CanvasApp";
import { positionBucket, positionBucketKey } from "./canvasStorage";

const sessions: ExtensionSessionSummary[] = [
  {
    id: "conv_1",
    title: "One",
    status: "running",
    workspace: "/workspace/one",
    createdAt: 1,
    updatedAt: 2,
  },
  {
    id: "conv_2",
    title: "Two",
    status: "idle",
    workspace: "/workspace/two",
    createdAt: 1,
    updatedAt: 1,
  },
];

function contextWith(items: ExtensionSessionSummary[] = sessions): {
  context: ExtensionContext;
  openSession: ReturnType<typeof vi.fn>;
  values: Map<string, unknown>;
} {
  const values = new Map<string, unknown>();
  const openSession = vi.fn(async () => undefined);
  const context = {
    capabilities: [
      "navigation.openSession",
      "navigation.openNewSession",
      "sessions.listPage",
    ],
    navigation: {
      openSession,
      openNewSession: vi.fn(async () => undefined),
    },
    sessions: {
      listAll: vi.fn(async () => items),
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

  it("keeps existing card positions when refresh adds a session", async () => {
    const { context } = contextWith();
    vi.mocked(context.sessions.listAll)
      .mockResolvedValueOnce(sessions)
      .mockResolvedValueOnce([
        ...sessions,
        {
          id: "conv_3",
          title: "Three",
          status: "idle",
          workspace: "/workspace/three",
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

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await screen.findByText("3 sessions");
    const after = Object.fromEntries(
      (
        flowProps.current?.nodes as Array<{ id: string; position: unknown }>
      ).map((node) => [node.id, node.position]),
    );
    expect(after.conv_1).toEqual(before.conv_1);
    expect(after.conv_2).toEqual(before.conv_2);
    expect(after.conv_3).toBeDefined();
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

  it("restores dragging when a mobile viewport becomes desktop-sized", async () => {
    let matches = false;
    let listener: (() => void) | undefined;
    vi.stubGlobal("matchMedia", () => ({
      get matches() {
        return matches;
      },
      addEventListener: (_event: string, next: () => void) => {
        listener = next;
      },
      removeEventListener: vi.fn(),
    }));
    const { context } = contextWith();
    render(<CanvasApp context={context} />);

    expect(
      await screen.findByRole("button", { name: "Arrange" }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(flowProps.current?.nodesDraggable).toBe(false);
    act(() => {
      matches = true;
      listener?.();
    });
    expect(
      screen.queryByRole("button", { name: "Arrange" }),
    ).not.toBeInTheDocument();
    expect(flowProps.current?.nodesDraggable).toBe(true);
  });

  it("shows explicit empty and initial error states", async () => {
    const empty = contextWith([]);
    const rendered = render(<CanvasApp context={empty.context} />);
    expect(
      await screen.findByText("No non-archived sessions"),
    ).toBeInTheDocument();
    rendered.unmount();

    const failed = contextWith();
    vi.mocked(failed.context.sessions.listAll).mockRejectedValue(
      new Error("offline"),
    );
    render(<CanvasApp context={failed.context} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  });
});
