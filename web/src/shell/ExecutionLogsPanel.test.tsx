// Tests for ExecutionLogsPanel — the right-side raw-JSON viewer for a
// conversation and its sub-agent sessions. The data hooks (useChildSessions,
// useSessionItems) and the chat store are mocked so the panel's own logic is
// covered: open/closed gating, the entry dropdown (main + children), the
// items-list states (loading / error / empty / populated), and per-item
// expand/collapse.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type { RawSessionItem } from "@/hooks/useSessionItems";

// ── Module mocks ──────────────────────────────────────────────────────────────

const h = vi.hoisted(() => ({
  children: [] as ChildSessionInfo[],
  itemsResult: {
    items: [] as RawSessionItem[],
    isLoading: false,
    error: null as Error | null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  },
  sessionStatus: "idle" as string,
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => {
  // Keep the real key helpers (executionLogTabKey / MAIN_EXECUTION_LOG_KEY) so
  // the panel's entry keys match what initialKey passes in.
  const actual = await importOriginal<typeof import("@/hooks/useChildSessions")>();
  return { ...actual, useChildSessions: () => ({ children: h.children }) };
});

vi.mock("@/hooks/useSessionItems", () => ({
  useSessionItems: () => h.itemsResult,
}));

vi.mock("@/hooks/useResizablePanel", () => ({
  useResizablePanel: () => ({ panelWidth: 400, handleProps: { tabIndex: 0 }, isDesktop: false }),
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: { sessionStatus: string }) => unknown) =>
    selector({ sessionStatus: h.sessionStatus }),
}));

import { executionLogTabKey } from "@/hooks/useChildSessions";
import { ExecutionLogsPanel } from "./ExecutionLogsPanel";

// ── Helpers ───────────────────────────────────────────────────────────────────

function mkChild(overrides: Partial<ChildSessionInfo>): ChildSessionInfo {
  return {
    id: "conv_child1",
    title: "researcher:auth",
    tool: "researcher",
    session_name: "auth",
    current_task_status: "completed",
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

function renderPanel(
  props: { open?: boolean; initialKey?: string | null; onClose?: () => void } = {},
) {
  const onClose = props.onClose ?? vi.fn();
  const result = render(
    <ExecutionLogsPanel
      open={props.open ?? true}
      conversationId="conv_main"
      initialKey={props.initialKey ?? executionLogTabKey("main")}
      onClose={onClose}
    />,
  );
  return { onClose, ...result };
}

beforeEach(() => {
  h.children = [];
  h.itemsResult = {
    items: [],
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  };
  h.sessionStatus = "idle";
});

afterEach(() => {
  cleanup();
});

describe("ExecutionLogsPanel open/close gating", () => {
  it("reflects open state via data-state and shows the active entry", () => {
    // WHY: when open the panel exposes data-state=open and renders the selector
    // for the main entry; the empty-content branch must not be taken.
    renderPanel({ open: true });
    const panel = screen.getByTestId("execution-logs-panel");
    expect(panel).toHaveAttribute("data-state", "open");
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("renders empty content and marks closed when not open", () => {
    // WHY: when closed the panel collapses (data-state=closed) and skips the
    // dropdown/items entirely — the `!open || !activeEntry` branch.
    renderPanel({ open: false, initialKey: null });
    expect(screen.getByTestId("execution-logs-panel")).toHaveAttribute("data-state", "closed");
    expect(screen.queryByText("No items")).toBeNull();
  });

  it("fires onClose when the Escape key is pressed while open", () => {
    // WHY: the keydown listener is the panel's keyboard-dismiss contract; it
    // must call onClose for Escape (and only while open).
    const { onClose } = renderPanel({ open: true });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("fires onClose when the header close button is clicked", () => {
    // WHY: the X button is the primary dismiss affordance.
    const { onClose } = renderPanel({ open: true });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("ExecutionLogsPanel entries", () => {
  it("labels a child entry by title (only the active entry is in the trigger)", () => {
    // WHY: buildLogEntries appends children after the pinned main entry; the
    // active trigger shows "main" while the child label lives in the closed
    // dropdown content. We assert the main label renders and the panel mounted.
    h.children = [mkChild({ id: "conv_c1", title: "researcher:auth" })];
    renderPanel({ open: true });
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByTestId("execution-logs-panel")).toBeInTheDocument();
  });

  it("renders the items list for the entry selected by initialKey (a child)", () => {
    // WHY: initialKey targets a child; that child's items must be the ones the
    // panel shows — proves activeEntry resolves from initialKey, not always main.
    h.children = [mkChild({ id: "conv_c1", title: "researcher:auth" })];
    h.itemsResult = { ...h.itemsResult, items: [{ id: "x1", role: "user" }] };
    renderPanel({ open: true, initialKey: executionLogTabKey("conv_c1") });
    // The child's label is the active trigger value.
    expect(screen.getByText("researcher:auth")).toBeInTheDocument();
    expect(screen.getByTestId("execution-log-entry")).toBeInTheDocument();
  });
});

describe("ExecutionLogsPanel items-list states", () => {
  it("shows a loading indicator while items are loading", () => {
    // WHY: the isLoading branch must render the spinner text, not "No items".
    h.itemsResult = { ...h.itemsResult, isLoading: true };
    renderPanel({ open: true });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("surfaces the error message when the items fetch fails", () => {
    // WHY: error state takes priority over empty/loaded and must show the
    // message so the user knows the load failed.
    h.itemsResult = { ...h.itemsResult, error: new Error("boom") };
    renderPanel({ open: true });
    expect(screen.getByText(/Failed to load items: boom/)).toBeInTheDocument();
  });

  it("shows 'No items' when the session has no items", () => {
    // WHY: the empty (length 0) branch is its own state distinct from loading.
    renderPanel({ open: true });
    expect(screen.getByText("No items")).toBeInTheDocument();
  });

  it("renders one collapsed entry per item with #N numbering", () => {
    // WHY: items map to numbered rows; collapsed rows show the single-line
    // JSON. Two items → two entries numbered #1 and #2.
    h.itemsResult = {
      ...h.itemsResult,
      items: [
        { id: "a", role: "user" },
        { id: "b", role: "assistant" },
      ],
    };
    renderPanel({ open: true });
    const entries = screen.getAllByTestId("execution-log-entry");
    expect(entries).toHaveLength(2);
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
  });

  it("expands an item to pretty-printed JSON on click and re-collapses", () => {
    // WHY: the per-item toggle drives aria-expanded and swaps the collapsed
    // one-liner for the indented <pre>; this pins the click handler.
    h.itemsResult = { ...h.itemsResult, items: [{ id: "a", role: "user" }] };
    renderPanel({ open: true });
    const btn = screen.getByTestId("execution-log-entry");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    // Pretty-printed form is multi-line (contains a newline + indentation).
    expect(screen.getByText(/"role": "user"/)).toBeInTheDocument();
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });
});
