import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ChildSessionInfo, useChildSessions } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import {
  buildTree,
  childActivity,
  computeSubtreeWidths,
  layoutTree,
  type TreeNode,
} from "./subagentGraphLayout";
import { SubagentsPanel } from "./SubagentsPanel";

// ---------------------------------------------------------------------------
// Mocks — SubagentsGraphView imports @xyflow/react which OOMs in jsdom.
// Mock the entire graph component so the toggle integration tests work
// without loading ReactFlow. The pure layout functions are tested directly
// from subagentGraphLayout.ts which has no @xyflow/react dependency.
// ---------------------------------------------------------------------------

vi.mock("./SubagentsGraphView", () => ({
  SubagentsGraphView: (props: Record<string, unknown>) => (
    <div
      data-testid="subagents-graph-view"
      data-conversation-id={props.conversationId}
      data-root-session-id={props.rootSessionId}
    />
  ),
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useChildSessions")>()),
  useChildSessions: vi.fn(),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(),
}));

vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="claude" />,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="codex" />,
}));
vi.mock("@/components/icons/OpenCodeIcon", () => ({
  OpenCodeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="opencode" />,
}));
vi.mock("@/components/icons/PiIcon", () => ({
  PiIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="pi" />,
}));
vi.mock("@/components/icons/OttoIcon", () => ({
  OttoIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="otto" />,
}));

const useChildSessionsMock = vi.mocked(useChildSessions);
const useSessionMock = vi.mocked(useSession);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function childInfo(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
  return {
    title: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

function mockChildTree(tree: Record<string, ChildSessionInfo[]>) {
  useChildSessionsMock.mockImplementation((id) => ({
    children: id !== null ? (tree[id] ?? []) : [],
    isLoading: false,
    error: null,
  }));
}

function defaultSession() {
  return {
    session: {
      id: "conv_root",
      agentId: "ag_root",
      agentName: null,
      runnerId: null,
      status: "idle",
      createdAt: 0,
      title: null,
      labels: {},
      items: [],
      pendingElicitations: [],
      permissionLevel: 4,
      parentSessionId: null,
      subAgentName: null,
    },
    isLoading: false,
    error: null,
  };
}

function renderPanel(opts: { conversationId?: string; rootSessionId?: string } = {}) {
  return render(
    <MemoryRouter>
      <SubagentsPanel
        conversationId={opts.conversationId ?? "conv_root"}
        rootSessionId={opts.rootSessionId ?? "conv_root"}
      />
    </MemoryRouter>,
  );
}

function leaf(id: string, overrides: Partial<TreeNode> = {}): TreeNode {
  return {
    id,
    label: id,
    activity: "idle",
    statusLabel: "Idle",
    preview: null,
    children: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  useChildSessionsMock.mockReset();
  useSessionMock.mockReset();
  useSessionMock.mockReturnValue(defaultSession() as ReturnType<typeof useSession>);
});

afterEach(cleanup);

// ===========================================================================
// Unit tests: childActivity
// ===========================================================================

describe("childActivity", () => {
  it("returns awaiting when pending_elicitations_count > 0, even if busy", () => {
    const result = childActivity(childInfo({ id: "x", busy: true, pending_elicitations_count: 2 }));
    expect(result).toEqual({ activity: "awaiting", label: "Needs response" });
  });

  it("returns launching for launching status", () => {
    const result = childActivity(childInfo({ id: "x", current_task_status: "launching" }));
    expect(result).toEqual({ activity: "launching", label: "Launching" });
  });

  it("returns working when busy", () => {
    const result = childActivity(childInfo({ id: "x", busy: true }));
    expect(result).toEqual({ activity: "working", label: "Working" });
  });

  it("returns failed when last_task_error is set", () => {
    const result = childActivity(
      childInfo({ id: "x", last_task_error: { code: "err", message: "boom" } }),
    );
    expect(result).toEqual({ activity: "failed", label: "Failed" });
  });

  it("returns failed for failed task status", () => {
    const result = childActivity(childInfo({ id: "x", current_task_status: "failed" }));
    expect(result).toEqual({ activity: "failed", label: "Failed" });
  });

  it("returns done for completed task status", () => {
    const result = childActivity(childInfo({ id: "x", current_task_status: "completed" }));
    expect(result).toEqual({ activity: "done", label: "Done" });
  });

  it("returns the raw status as label for unknown statuses", () => {
    const result = childActivity(childInfo({ id: "x", current_task_status: "cancelled" }));
    expect(result).toEqual({ activity: "other", label: "cancelled" });
  });

  it("returns idle when no status signals are set", () => {
    const result = childActivity(childInfo({ id: "x" }));
    expect(result).toEqual({ activity: "idle", label: "Idle" });
  });

  it("prioritizes awaiting over launching", () => {
    const result = childActivity(
      childInfo({ id: "x", current_task_status: "launching", pending_elicitations_count: 1 }),
    );
    expect(result.activity).toBe("awaiting");
  });
});

// ===========================================================================
// Unit tests: computeSubtreeWidths
// ===========================================================================

describe("computeSubtreeWidths", () => {
  it("assigns NODE_WIDTH to a leaf", () => {
    const widths = computeSubtreeWidths(leaf("a"));
    expect(widths.get("a")).toBe(180);
  });

  it("computes width as sum of children + gaps for a parent", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c1"), leaf("c2"), leaf("c3")],
    };
    const widths = computeSubtreeWidths(root);
    // 3 * 180 + 2 * 40 = 620
    expect(widths.get("root")).toBe(620);
    expect(widths.get("c1")).toBe(180);
  });

  it("uses the larger of NODE_WIDTH and total children width", () => {
    const root: TreeNode = { ...leaf("root"), children: [leaf("c1")] };
    const widths = computeSubtreeWidths(root);
    expect(widths.get("root")).toBe(180);
  });

  it("handles nested trees", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [{ ...leaf("a"), children: [leaf("a1"), leaf("a2")] }, leaf("b")],
    };
    const widths = computeSubtreeWidths(root);
    // a subtree: 2 * 180 + 40 = 400
    expect(widths.get("a")).toBe(400);
    // root: 400 + 180 + 40 = 620
    expect(widths.get("root")).toBe(620);
  });
});

// ===========================================================================
// Unit tests: layoutTree
// ===========================================================================

describe("layoutTree", () => {
  it("places a single root node at the origin", () => {
    const { nodes, edges } = layoutTree(leaf("root"), "root");
    expect(nodes).toHaveLength(1);
    expect(edges).toHaveLength(0);
    expect(nodes[0].id).toBe("root");
    expect(nodes[0].data.isActive).toBe(true);
  });

  it("creates edges from parent to each child", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c1"), leaf("c2")],
    };
    const { nodes, edges } = layoutTree(root, "none");
    expect(nodes).toHaveLength(3);
    expect(edges).toHaveLength(2);
    expect(edges[0]).toMatchObject({ source: "root", target: "c1" });
    expect(edges[1]).toMatchObject({ source: "root", target: "c2" });
  });

  it("marks active node correctly", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c1"), leaf("c2")],
    };
    const { nodes } = layoutTree(root, "c2");
    expect(nodes.find((n) => n.id === "root")!.data.isActive).toBe(false);
    expect(nodes.find((n) => n.id === "c1")!.data.isActive).toBe(false);
    expect(nodes.find((n) => n.id === "c2")!.data.isActive).toBe(true);
  });

  it("positions children below the parent", () => {
    const root: TreeNode = { ...leaf("root"), children: [leaf("c1")] };
    const { nodes } = layoutTree(root, "none");
    const rootNode = nodes.find((n) => n.id === "root")!;
    const childNode = nodes.find((n) => n.id === "c1")!;
    expect(childNode.position.y).toBeGreaterThan(rootNode.position.y);
  });

  it("spaces siblings horizontally", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c1"), leaf("c2"), leaf("c3")],
    };
    const { nodes } = layoutTree(root, "none");
    const xs = ["c1", "c2", "c3"].map((id) => nodes.find((n) => n.id === id)!.position.x);
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
  });

  it("centers parent above its children", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c1"), leaf("c2")],
    };
    const { nodes } = layoutTree(root, "none");
    const rootX = nodes.find((n) => n.id === "root")!.position.x;
    const c1X = nodes.find((n) => n.id === "c1")!.position.x;
    const c2X = nodes.find((n) => n.id === "c2")!.position.x;
    const rootCenter = rootX + 180 / 2;
    const childrenCenter = (c1X + c2X) / 2 + 180 / 2;
    expect(Math.abs(rootCenter - childrenCenter)).toBeLessThan(1);
  });

  it("animates edges to working children", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c_work", { activity: "working" }), leaf("c_idle")],
    };
    const { edges } = layoutTree(root, "none");
    const workEdge = edges.find((e) => e.target === "c_work")!;
    const idleEdge = edges.find((e) => e.target === "c_idle")!;
    expect(workEdge.animated).toBe(true);
    expect(idleEdge.animated).toBe(false);
  });

  it("uses higher opacity on edges to working children", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [leaf("c_work", { activity: "working" }), leaf("c_idle")],
    };
    const { edges } = layoutTree(root, "none");
    const workEdge = edges.find((e) => e.target === "c_work")!;
    const idleEdge = edges.find((e) => e.target === "c_idle")!;
    expect(workEdge.style.opacity).toBeGreaterThan(idleEdge.style.opacity as number);
  });

  it("lays out a 3-level tree without overlapping nodes", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: [
        { ...leaf("a"), children: [leaf("a1"), leaf("a2")] },
        { ...leaf("b"), children: [leaf("b1"), leaf("b2"), leaf("b3")] },
      ],
    };
    const { nodes } = layoutTree(root, "none");
    const byY = new Map<number, typeof nodes>();
    for (const n of nodes) {
      const y = n.position.y;
      if (!byY.has(y)) byY.set(y, []);
      byY.get(y)!.push(n);
    }
    for (const [, levelNodes] of byY) {
      const sorted = levelNodes.sort((a, b) => a.position.x - b.position.x);
      for (let i = 1; i < sorted.length; i++) {
        const prevRight = sorted[i - 1].position.x + 180;
        const currLeft = sorted[i].position.x;
        expect(currLeft).toBeGreaterThanOrEqual(prevRight);
      }
    }
  });

  it("handles a wide flat tree (many siblings)", () => {
    const root: TreeNode = {
      ...leaf("root"),
      children: Array.from({ length: 10 }, (_, i) => leaf(`c${i}`)),
    };
    const { nodes, edges } = layoutTree(root, "none");
    expect(nodes).toHaveLength(11);
    expect(edges).toHaveLength(10);
    // All children at the same y level
    const childYs = nodes.filter((n) => n.id !== "root").map((n) => n.position.y);
    expect(new Set(childYs).size).toBe(1);
  });
});

// ===========================================================================
// Unit tests: buildTree
// ===========================================================================

describe("buildTree", () => {
  it("creates a leaf when no children exist in the map", () => {
    const tree = buildTree("root", "main", "idle", "Idle", null, new Map(), 0);
    expect(tree).toEqual({
      id: "root",
      label: "main",
      activity: "idle",
      statusLabel: "Idle",
      preview: null,
      children: [],
    });
  });

  it("builds nested tree from children map", () => {
    const map = new Map<string, ChildSessionInfo[]>();
    map.set("root", [
      childInfo({ id: "c1", session_name: "auth", tool: "researcher", busy: true }),
      childInfo({
        id: "c2",
        session_name: "docs",
        tool: "writer",
        current_task_status: "completed",
      }),
    ]);
    map.set("c1", [childInfo({ id: "c1a", session_name: "search", tool: "Explore" })]);

    const tree = buildTree("root", "main", "working", "Working", null, map, 0);

    expect(tree.children).toHaveLength(2);
    expect(tree.children[0].id).toBe("c1");
    expect(tree.children[0].label).toBe("auth");
    expect(tree.children[0].activity).toBe("working");
    expect(tree.children[0].children).toHaveLength(1);
    expect(tree.children[0].children[0].id).toBe("c1a");
    expect(tree.children[1].id).toBe("c2");
    expect(tree.children[1].activity).toBe("done");
    expect(tree.children[1].children).toHaveLength(0);
  });

  it("stops recursion at MAX_TREE_DEPTH", () => {
    const map = new Map<string, ChildSessionInfo[]>();
    map.set("root", [childInfo({ id: "c1", tool: "a" })]);
    map.set("c1", [childInfo({ id: "c2", tool: "b" })]);
    map.set("c2", [childInfo({ id: "c3", tool: "c" })]);
    map.set("c3", [childInfo({ id: "c4", tool: "d" })]);

    const tree = buildTree("root", "main", "idle", "Idle", null, map, 0);

    expect(tree.children).toHaveLength(1);
    expect(tree.children[0].children).toHaveLength(1);
    expect(tree.children[0].children[0].children).toHaveLength(1);
    expect(tree.children[0].children[0].children[0].children).toHaveLength(0);
  });

  it("uses session_name, then title, then tool, then id as label", () => {
    const map = new Map<string, ChildSessionInfo[]>();
    map.set("root", [
      childInfo({ id: "c1", session_name: "named" }),
      childInfo({ id: "c2", title: "titled" }),
      childInfo({ id: "c3", tool: "tooled" }),
      childInfo({ id: "c4" }),
    ]);

    const tree = buildTree("root", "main", "idle", "Idle", null, map, 0);

    expect(tree.children[0].label).toBe("named");
    expect(tree.children[1].label).toBe("titled");
    expect(tree.children[2].label).toBe("tooled");
    expect(tree.children[3].label).toBe("c4");
  });

  it("passes through last_message_preview", () => {
    const map = new Map<string, ChildSessionInfo[]>();
    map.set("root", [
      childInfo({ id: "c1", tool: "a", last_message_preview: "Searching for auth..." }),
    ]);

    const tree = buildTree("root", "main", "idle", "Idle", null, map, 0);

    expect(tree.children[0].preview).toBe("Searching for auth...");
  });
});

// ===========================================================================
// Integration tests: view-mode toggle
// ===========================================================================

describe("SubagentsPanel view-mode toggle", () => {
  it("defaults to list view", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    renderPanel();

    expect(screen.getByTestId("view-mode-list")).toBeInTheDocument();
    expect(screen.getByTestId("view-mode-graph")).toBeInTheDocument();
    expect(screen.getByTestId("subagent-main-row")).toBeInTheDocument();
    expect(screen.queryByTestId("subagents-graph-view")).toBeNull();
  });

  it("switches to graph view when the graph button is clicked", async () => {
    mockChildTree({
      conv_root: [
        childInfo({ id: "c1", tool: "researcher", session_name: "auth", busy: true }),
        childInfo({
          id: "c2",
          tool: "Explore",
          session_name: "files",
          current_task_status: "completed",
        }),
      ],
    });

    renderPanel();

    fireEvent.click(screen.getByTestId("view-mode-graph"));

    await waitFor(() => {
      expect(screen.getByTestId("subagents-graph-view")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("subagent-main-row")).toBeNull();
    expect(screen.queryByTestId("subagent-row")).toBeNull();
  });

  it("switches back to list view when the list button is clicked", async () => {
    mockChildTree({
      conv_root: [childInfo({ id: "c1", tool: "researcher" })],
    });

    renderPanel();

    fireEvent.click(screen.getByTestId("view-mode-graph"));
    await waitFor(() => {
      expect(screen.getByTestId("subagents-graph-view")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("view-mode-list"));
    expect(screen.getByTestId("subagent-main-row")).toBeInTheDocument();
    expect(screen.queryByTestId("subagents-graph-view")).toBeNull();
  });

  it("passes conversationId and rootSessionId to the graph view", async () => {
    mockChildTree({
      conv_root: [childInfo({ id: "c1", tool: "researcher" })],
    });

    renderPanel({ conversationId: "conv_child", rootSessionId: "conv_root" });

    fireEvent.click(screen.getByTestId("view-mode-graph"));

    const graph = await screen.findByTestId("subagents-graph-view");
    expect(graph).toHaveAttribute("data-conversation-id", "conv_child");
    expect(graph).toHaveAttribute("data-root-session-id", "conv_root");
  });

  it("preserves the toggle bar across both views", async () => {
    mockChildTree({
      conv_root: [childInfo({ id: "c1", tool: "researcher" })],
    });

    renderPanel();

    // List view has toggle bar
    expect(screen.getByTestId("view-mode-list")).toBeInTheDocument();
    expect(screen.getByTestId("view-mode-graph")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("view-mode-graph"));

    await waitFor(() => {
      expect(screen.getByTestId("subagents-graph-view")).toBeInTheDocument();
    });

    // Graph view still has toggle bar
    expect(screen.getByTestId("view-mode-list")).toBeInTheDocument();
    expect(screen.getByTestId("view-mode-graph")).toBeInTheDocument();
  });
});
