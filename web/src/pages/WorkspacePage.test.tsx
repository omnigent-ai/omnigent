import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useWorkspaceLayoutStore } from "@/store/workspaceLayout";
import { WorkspacePage } from "./WorkspacePage";

const chatStoreMocks = vi.hoisted(() => ({ switchTo: vi.fn(), loadInBackground: vi.fn() }));

let resizeObserverCallback: ResizeObserverCallback | null = null;

class StubResizeObserver {
  constructor(callback: ResizeObserverCallback) {
    resizeObserverCallback = callback;
  }

  observe() {}
  unobserve() {}
  disconnect() {}
}

function resizeWorkspace(width: number) {
  act(() => {
    resizeObserverCallback?.(
      [{ contentRect: { width } } as ResizeObserverEntry],
      {} as ResizeObserver,
    );
  });
}

vi.mock("@/store/chatStore", () => ({
  ChatStoreScopeProvider: ({ children }: { children: ReactNode }) => children,
  useChatStore: {
    getState: () => ({
      switchTo: chatStoreMocks.switchTo,
      loadInBackground: chatStoreMocks.loadInBackground,
    }),
  },
}));

vi.mock("@dnd-kit/core", () => ({
  useDroppable: () => ({ setNodeRef: vi.fn(), isOver: false }),
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({
    data: {
      pages: [
        {
          data: [
            { id: "session-a", title: "First task" },
            { id: "session-b", title: "Second task" },
          ],
        },
      ],
    },
  }),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));

vi.mock("@/shell/SessionWorkspaceDock", () => ({
  SessionWorkspaceDock: ({ conversationId, label }: { conversationId: string; label: string }) => (
    <aside aria-label={`Workspace for ${label}`} data-conversation-id={conversationId} />
  ),
}));

vi.mock("./ChatPage", () => ({
  ChatPage: ({ conversationId, active = true }: { conversationId?: string; active?: boolean }) => (
    <div data-testid={`chat-${conversationId ?? "landing"}`} data-active={String(active)}>
      {conversationId ?? "landing"}
    </div>
  ),
}));

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}

function renderWorkspace(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/" element={<WorkspacePage />} />
        <Route path="/c/:conversationId" element={<WorkspacePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkspacePage", () => {
  beforeEach(() => {
    resizeObserverCallback = null;
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    localStorage.clear();
    useWorkspaceLayoutStore.getState().reset("session-a");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders independent panes, focuses by pointer, and collapses on close", async () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));
    const split = useWorkspaceLayoutStore.getState();
    if (split.root.kind !== "split") throw new Error("expected split root");
    const firstPaneId = split.root.children[0].id;
    const secondPaneId = split.root.children[1].id;

    renderWorkspace("/c/session-b");

    expect(screen.getByTestId("chat-session-a")).toHaveAttribute("data-active", "false");
    expect(screen.getByTestId("chat-session-b")).toHaveAttribute("data-active", "true");

    fireEvent.pointerDown(screen.getByTestId(`workspace-pane-${firstPaneId}`));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/c/session-a"));
    expect(screen.getByTestId("chat-session-a")).toHaveAttribute("data-active", "true");

    fireEvent.click(screen.getByLabelText(`Close pane session-b`));
    await waitFor(() => expect(screen.queryByTestId(`workspace-pane-${secondPaneId}`)).toBeNull());
    expect(screen.getByTestId("chat-session-a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Close pane/ })).toBeNull();
  });

  it("binds a background stream for panes that are not focused", async () => {
    // After a reload only the route conversation is live; the other pane must
    // hydrate in the background instead of painting the focused transcript.
    chatStoreMocks.loadInBackground.mockClear();
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    const pinSpy = vi.spyOn(conversationRegistry, "pin");
    const unpinSpy = vi.spyOn(conversationRegistry, "unpin");
    const { unmount } = renderWorkspace("/c/session-b");

    await waitFor(() => expect(chatStoreMocks.loadInBackground).toHaveBeenCalledWith("session-a"));
    expect(chatStoreMocks.loadInBackground).not.toHaveBeenCalledWith("session-b");

    // Both pane conversations are pinned against stream-slot eviction while
    // visible, and released once the workspace unmounts.
    expect(pinSpy).toHaveBeenCalledWith("session-a");
    expect(pinSpy).toHaveBeenCalledWith("session-b");
    unmount();
    expect(unpinSpy).toHaveBeenCalledWith("session-a");
    expect(unpinSpy).toHaveBeenCalledWith("session-b");
    pinSpy.mockRestore();
    unpinSpy.mockRestore();
  });

  it("labels each pane with its session title when split", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));
    const split = useWorkspaceLayoutStore.getState();
    if (split.root.kind !== "split") throw new Error("expected split root");
    const firstPaneId = split.root.children[0].id;
    const secondPaneId = split.root.children[1].id;

    renderWorkspace("/c/session-b");

    expect(screen.getByTestId(`pane-title-${firstPaneId}`)).toHaveTextContent("First task");
    expect(screen.getByTestId(`pane-title-${secondPaneId}`)).toHaveTextContent("Second task");
  });

  it("renders one corresponding workspace dock for every split chat", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    renderWorkspace("/c/session-b");

    expect(screen.getByRole("complementary", { name: "Workspace for First task" })).toHaveAttribute(
      "data-conversation-id",
      "session-a",
    );
    expect(
      screen.getByRole("complementary", { name: "Workspace for Second task" }),
    ).toHaveAttribute("data-conversation-id", "session-b");
  });

  it("toggles each chat's workspace independently from its pane header", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    renderWorkspace("/c/session-b");

    fireEvent.click(screen.getByRole("button", { name: "Collapse workspace for First task" }));

    expect(screen.queryByRole("complementary", { name: "Workspace for First task" })).toBeNull();
    expect(
      screen.getByRole("complementary", { name: "Workspace for Second task" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Expand workspace for First task" }),
    ).toBeInTheDocument();
  });

  it("reflows each chat's own dock below or right at the SP2K column threshold", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    renderWorkspace("/c/session-b");

    for (const sessionId of ["session-a", "session-b"]) {
      const column = screen.getByTestId(`session-column-${sessionId}`);
      expect(column.className).toContain("@container/session-column");
      const dockLayout = screen.getByTestId(`session-dock-${sessionId}`);
      expect(dockLayout.className).toContain("flex-col");
      expect(dockLayout.className).toContain("@min-[720px]/session-column:flex-row");
    }
  });

  it("stacks a horizontal chat split below two SP2K minimum columns", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));
    const split = useWorkspaceLayoutStore.getState().root;
    if (split.kind !== "split") throw new Error("expected split root");

    renderWorkspace("/c/session-b");

    const splitLayout = screen.getByTestId(`workspace-split-layout-${split.id}`);
    expect(splitLayout.className).toContain("flex-col");
    expect(splitLayout.className).toContain("@min-[720px]/workspace-split:flex-row");

    resizeWorkspace(719);

    expect(splitLayout).toHaveAttribute("data-effective-direction", "vertical");
    const separator = screen.getByRole("separator", { name: "Resize horizontal split" });
    expect(separator).toHaveAttribute("aria-orientation", "horizontal");
    fireEvent.keyDown(separator, { key: "ArrowDown" });
    expect(useWorkspaceLayoutStore.getState().root).toMatchObject({ sizes: [55, 45] });
    expect(screen.getByRole("complementary", { name: "Workspace for First task" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Workspace for Second task" })).toBeVisible();
  });

  it("restores a horizontal split and horizontal resizing at 720px", () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));
    const split = useWorkspaceLayoutStore.getState().root;
    if (split.kind !== "split") throw new Error("expected split root");

    renderWorkspace("/c/session-b");
    resizeWorkspace(720);

    expect(screen.getByTestId(`workspace-split-layout-${split.id}`)).toHaveAttribute(
      "data-effective-direction",
      "horizontal",
    );
    const separator = screen.getByRole("separator", { name: "Resize horizontal split" });
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(useWorkspaceLayoutStore.getState().root).toMatchObject({ sizes: [55, 45] });
  });

  it("highlights only the focused pane and moves the highlight on pointer focus", async () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    const { container } = renderWorkspace("/c/session-b");
    const unfocusedPane = container.querySelector('[data-workspace-pane-id][data-focused="false"]');
    const focusedPane = container.querySelector('[data-workspace-pane-id][data-focused="true"]');
    expect(unfocusedPane).not.toBeNull();
    expect(focusedPane).not.toBeNull();
    expect(unfocusedPane).not.toHaveClass("after:ring-1", "after:ring-primary/40");
    expect(focusedPane).toHaveClass(
      "after:pointer-events-none",
      "after:absolute",
      "after:inset-0",
      "after:z-50",
      "after:ring-1",
      "after:ring-inset",
      "after:ring-primary/40",
    );

    fireEvent.pointerDown(unfocusedPane!);
    await waitFor(() => {
      expect(unfocusedPane).toHaveAttribute("data-focused", "true");
      expect(unfocusedPane).toHaveClass("after:ring-1", "after:ring-primary/40");
      expect(focusedPane).not.toHaveClass("after:ring-1", "after:ring-primary/40");
    });

    expect(unfocusedPane?.closest(".pt-14")).toBeNull();
  });

  it("omits pane chrome for a single pane", () => {
    renderWorkspace("/c/session-a");
    expect(screen.queryByTestId(/pane-title-/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Close pane/ })).toBeNull();
  });

  it("exposes five directional drop zones per pane, including center", () => {
    const paneId = useWorkspaceLayoutStore.getState().root.id;
    renderWorkspace("/c/session-a");
    for (const edge of ["left", "right", "top", "bottom", "center"]) {
      expect(screen.getByTestId(`drop-zone-${paneId}-${edge}`)).toBeInTheDocument();
    }
  });

  it("keeps the landing and ordinary single-session routes unsplit", async () => {
    const { unmount } = renderWorkspace("/");
    expect(screen.getByTestId("chat-landing")).toBeInTheDocument();

    unmount();
    renderWorkspace("/c/session-c");

    await waitFor(() => expect(screen.getByTestId("chat-session-c")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Close pane/ })).toBeNull();
  });
});
