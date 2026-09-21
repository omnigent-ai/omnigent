import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  WORKSPACE_LAYOUT_STORAGE_KEY,
  closeWorkspacePane,
  createWorkspaceLayout,
  findWorkspaceLeaf,
  findWorkspaceLeafBySession,
  setWorkspaceLeafSession,
  readWorkspaceLayout,
  resizeWorkspaceSplit,
  selectWorkspaceSession,
  splitWorkspacePane,
  useWorkspaceLayoutStore,
  writeWorkspaceLayout,
  type WorkspaceNode,
} from "./workspaceLayout";

describe("setWorkspaceLeafSession", () => {
  it("replaces the target leaf's session and focuses it", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");
    const leftPaneId = split.root.kind === "split" ? split.root.children[0].id : "";

    const next = setWorkspaceLeafSession(split, leftPaneId, "session-c");
    expect(findWorkspaceLeaf(next.root, leftPaneId)?.sessionId).toBe("session-c");
    expect(next.focusedPaneId).toBe(leftPaneId);
  });

  it("focuses the existing leaf instead of duplicating a session", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");
    const leftPaneId = split.root.kind === "split" ? split.root.children[0].id : "";
    const rightPaneId = split.root.kind === "split" ? split.root.children[1].id : "";

    const next = setWorkspaceLeafSession(split, leftPaneId, "session-b");
    expect(findWorkspaceLeaf(next.root, leftPaneId)?.sessionId).toBe("session-a");
    expect(findWorkspaceLeafBySession(next.root, "session-b")?.id).toBe(rightPaneId);
    expect(next.focusedPaneId).toBe(rightPaneId);
  });
});

describe("id generation after restore", () => {
  it("keeps node ids unique across a simulated reload", async () => {
    // A real reload re-evaluates the module: a process-local id sequence
    // restarts while the persisted tree keeps its ids, so generation must
    // derive from the restored tree.
    localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        root: {
          kind: "split",
          id: "split-1",
          direction: "horizontal",
          children: [
            { kind: "leaf", id: "pane-1", sessionId: "session-a" },
            { kind: "leaf", id: "pane-2", sessionId: "session-b" },
          ],
          sizes: [50, 50],
        },
        focusedPaneId: "pane-1",
      }),
    );

    vi.resetModules();
    const reloaded = await import("./workspaceLayout");
    const restored = reloaded.readWorkspaceLayout();
    if (!restored || restored.root.kind !== "split") throw new Error("expected restored split");

    const next = reloaded.splitWorkspacePane(restored, "pane-1", "session-c", "bottom");

    const ids: string[] = [];
    const collect = (node: WorkspaceNode) => {
      ids.push(node.id);
      if (node.kind === "split") {
        collect(node.children[0]);
        collect(node.children[1]);
      }
    };
    collect(next.root);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("workspace layout transitions", () => {
  it("creates horizontal and vertical splits around the target pane", () => {
    const initial = createWorkspaceLayout("session-a");
    const targetId = initial.root.id;

    const right = splitWorkspacePane(initial, targetId, "session-b", "right");
    expect(right.root).toMatchObject({
      kind: "split",
      direction: "horizontal",
      sizes: [50, 50],
      children: [
        { kind: "leaf", sessionId: "session-a" },
        { kind: "leaf", sessionId: "session-b" },
      ],
    });
    expect(findWorkspaceLeaf(right.root, right.focusedPaneId)?.sessionId).toBe("session-b");

    const leftPaneId = right.root.kind === "split" ? right.root.children[0].id : "";
    const top = splitWorkspacePane(right, leftPaneId, "session-c", "top");
    expect(top.root).toMatchObject({
      kind: "split",
      direction: "horizontal",
      children: [
        {
          kind: "split",
          direction: "vertical",
          children: [
            { kind: "leaf", sessionId: "session-c" },
            { kind: "leaf", sessionId: "session-a" },
          ],
        },
        { kind: "leaf", sessionId: "session-b" },
      ],
    });
  });

  it("selects existing sessions, replaces the focused pane, and avoids duplicates", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");

    const selectedExisting = selectWorkspaceSession(split, "session-a");
    expect(
      findWorkspaceLeaf(selectedExisting.root, selectedExisting.focusedPaneId)?.sessionId,
    ).toBe("session-a");

    const replaced = selectWorkspaceSession(selectedExisting, "session-c");
    expect(findWorkspaceLeaf(replaced.root, replaced.focusedPaneId)?.sessionId).toBe("session-c");
    expect(JSON.stringify(replaced.root)).not.toContain("session-a");

    const duplicateDrop = splitWorkspacePane(
      replaced,
      replaced.focusedPaneId,
      "session-b",
      "bottom",
    );
    expect(duplicateDrop).toEqual(replaced);
  });

  it("collapses a split when a pane closes and keeps a valid focus", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");

    const closed = closeWorkspacePane(split, split.focusedPaneId);
    expect(closed.root).toMatchObject({ kind: "leaf", sessionId: "session-a" });
    expect(closed.focusedPaneId).toBe(closed.root.id);

    expect(closeWorkspacePane(closed, closed.root.id)).toEqual(closed);
  });

  it("clamps resized split ratios", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");
    if (split.root.kind !== "split") throw new Error("expected split root");

    expect(resizeWorkspaceSplit(split, split.root.id, 10).root).toMatchObject({ sizes: [20, 80] });
    expect(resizeWorkspaceSplit(split, split.root.id, 72).root).toMatchObject({ sizes: [72, 28] });
    expect(resizeWorkspaceSplit(split, split.root.id, 95).root).toMatchObject({ sizes: [80, 20] });
  });
});

describe("workspace layout persistence", () => {
  beforeEach(() => localStorage.clear());

  it("round-trips a versioned layout and rejects malformed state", () => {
    const initial = createWorkspaceLayout("session-a");
    const layout = splitWorkspacePane(initial, initial.root.id, "session-b", "bottom");

    writeWorkspaceLayout(layout);
    expect(readWorkspaceLayout()).toEqual(layout);

    localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify({ version: 1, root: null }));
    expect(readWorkspaceLayout()).toBeNull();
  });
});

describe("workspace layout store", () => {
  beforeEach(() => {
    localStorage.clear();
    useWorkspaceLayoutStore.getState().reset("session-a");
  });

  it("persists split, focus, resize, and close actions", () => {
    const initial = useWorkspaceLayoutStore.getState();
    useWorkspaceLayoutStore.getState().splitPane(initial.root.id, "session-b", "right");

    let state = useWorkspaceLayoutStore.getState();
    expect(findWorkspaceLeaf(state.root, state.focusedPaneId)?.sessionId).toBe("session-b");
    expect(readWorkspaceLayout()).toMatchObject({ root: state.root });

    if (state.root.kind !== "split") throw new Error("expected split root");
    useWorkspaceLayoutStore.getState().resizeSplit(state.root.id, 65);
    state = useWorkspaceLayoutStore.getState();
    expect(state.root).toMatchObject({ sizes: [65, 35] });

    const firstPaneId = state.root.kind === "split" ? state.root.children[0].id : "";
    useWorkspaceLayoutStore.getState().focusPane(firstPaneId);
    expect(useWorkspaceLayoutStore.getState().focusedPaneId).toBe(firstPaneId);

    useWorkspaceLayoutStore.getState().closePane(firstPaneId);
    state = useWorkspaceLayoutStore.getState();
    expect(state.root).toMatchObject({ kind: "leaf", sessionId: "session-b" });
  });
});
