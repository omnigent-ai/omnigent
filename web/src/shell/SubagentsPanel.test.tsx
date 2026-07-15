import { cleanup, render, screen, within } from "@testing-library/react";
import {
  BookOpenIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { type ChildSessionInfo, useChildSessions } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { iconForAgentType, SubagentsPanel } from "./SubagentsPanel";

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  // Keep the real module (MAX_TREE_DEPTH and friends) — only the
  // hook itself is replaced.
  ...(await importOriginal<typeof import("@/hooks/useChildSessions")>()),
  useChildSessions: vi.fn(),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(),
}));

// Stub the brand logos with plain SVGs so jsdom doesn't have to resolve
// @lobehub/ui's runtime tooltip imports. The assertions prove which icon
// the row selected, independent of the real glyph internals.
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="claude" />,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="codex" />,
}));
vi.mock("@/components/icons/OpenCodeIcon", () => ({
  OpenCodeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="opencode" />,
}));
vi.mock("@/components/icons/KiroIcon", () => ({
  KiroIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="kiro" />,
}));
// Same marker treatment for the local pi glyph so selection assertions stay uniform.
vi.mock("@/components/icons/PiIcon", () => ({
  PiIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="pi" />,
}));
// And for the Otto mascot — the generic sub-agent fallback icon.
vi.mock("@/components/icons/OttoIcon", () => ({
  OttoIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="otto" />,
}));

const useChildSessionsMock = vi.mocked(useChildSessions);
const useSessionMock = vi.mocked(useSession);

interface RenderOptions {
  /** The conversation in main — used only for active-row highlighting. */
  conversationId?: string;
  /** The root id whose children populate the list. */
  rootSessionId?: string;
}

function renderPanel({
  conversationId = "conv_parent",
  rootSessionId = "conv_parent",
  initialEntries,
}: RenderOptions & { initialEntries?: string[] } = {}) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
    </MemoryRouter>,
  );
}

/** Build a full ChildSessionInfo, defaulting the fields a given test
 *  doesn't care about so each case states only what it exercises. */
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

/** Look up a rendered child row by its session id. */
function childRow(container: HTMLElement, childId: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-child-session-id="${childId}"]`);
  if (!el) throw new Error(`subagent row ${childId} not rendered`);
  return el;
}

/** Point useChildSessions at an id-keyed tree of children. The panel
 *  fetches a list per rendered row (the tree levels), so ids absent
 *  from the map — every leaf the recursive rows probe — get no
 *  children, mirroring the real hook's empty default. */
function mockChildTree(tree: Record<string, ChildSessionInfo[]>) {
  useChildSessionsMock.mockImplementation((id) => ({
    children: id !== null ? (tree[id] ?? []) : [],
    isLoading: false,
    error: null,
  }));
}

/** Agent-type → category-icon expectations. Order-sensitive cases (review
 *  before code, test before code) guard the substring precedence. */
const ICON_CASES: Array<[string | null, ReturnType<typeof iconForAgentType>]> = [
  ["Explore", SearchIcon],
  ["deep-researcher", BookOpenIcon],
  ["planner", CompassIcon],
  ["architect", CompassIcon],
  ["code-reviewer", ScanSearchIcon],
  ["pr-test-analyzer", FlaskConicalIcon],
  ["frontend_engineer", Code2Icon],
  // Both halves of the doc/writ branch, so neither sub-condition can be
  // dropped without a test failing.
  ["documentation", FileTextIcon],
  ["technical-writer", FileTextIcon],
  ["general-purpose", OttoIcon],
  [null, OttoIcon],
];

beforeEach(() => {
  useChildSessionsMock.mockReset();
  useSessionMock.mockReset();
  // Default: parent's status is idle. Tests override per-case.
  useSessionMock.mockReturnValue({
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
  });
});

afterEach(cleanup);

describe("SubagentsPanel", () => {
  it("always renders a 'main' row linking to the root session", () => {
    // No children at all — the panel still shows the main link so
    // the user always has a path back to the parent.
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    renderPanel({ rootSessionId: "conv_root" });

    const main = screen.getByTestId("subagent-main-row");
    expect(main).toHaveAttribute("href", "/c/conv_root");
    expect(main).toHaveAttribute("data-root-session-id", "conv_root");
    expect(main).toHaveTextContent(/main/i);
    expect(screen.queryByTestId("subagent-row")).toBeNull();
  });

  it("labels the main row with the root agent's name when available", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: "deep-researcher",
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
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({ rootSessionId: "conv_root" });

    const main = screen.getByTestId("subagent-main-row");
    expect(main).toHaveTextContent("deep-researcher");
    expect(main).not.toHaveTextContent(/\bmain\b/);
  });

  it("labels native-wrapper main rows with the product name, not the YAML agent name", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: "claude-native-ui",
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: null,
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({ rootSessionId: "conv_root" });

    const main = screen.getByTestId("subagent-main-row");
    expect(main).toHaveTextContent("Claude Code");
    expect(main).not.toHaveTextContent("claude-native-ui");
  });

  it("labels Pi native-wrapper main rows with the product name", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: "pi-native-ui",
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: { "omnigent.wrapper": "pi-native-ui" },
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: null,
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({ rootSessionId: "conv_root" });

    const main = screen.getByTestId("subagent-main-row");
    expect(main).toHaveTextContent("Pi");
    expect(main).not.toHaveTextContent("pi-native-ui");
  });

  it("shows the root's latest message as the main-row preview", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {},
        items: [
          {
            id: "i1",
            type: "message",
            response_id: "r1",
            status: "completed",
            data: {
              role: "assistant",
              content: [{ type: "output_text", text: "Hello! How can I help you today?" }],
            },
          },
          // A trailing tool call after the message — the preview must skip it
          // and surface the latest *message* text, not the call structure.
          {
            id: "i2",
            type: "function_call",
            response_id: "r1",
            status: "completed",
            data: { name: "sys_os_read", arguments: "{}", call_id: "c1" },
          },
        ],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: null,
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({ rootSessionId: "conv_root" });

    expect(screen.getByTestId("subagent-main-preview")).toHaveTextContent(
      "Hello! How can I help you today?",
    );
  });

  it("omits the main-row preview when the root has no message yet", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    // Default useSession mock (beforeEach) returns items: [] — nothing to preview.
    renderPanel({ rootSessionId: "conv_root" });
    expect(screen.queryByTestId("subagent-main-preview")).toBeNull();
  });

  it("renders an 'Add agent' button, with the dialog mounted only after a click", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    renderPanel({ rootSessionId: "conv_root" });

    expect(screen.getByTestId("add-agent-button")).toHaveTextContent(/add agent/i);
    // The dialog is mounted lazily (on click) so the closed rail carries
    // none of the dialog's query dependencies — assert it's absent here.
    // (The dialog's own behavior is covered by AddAgentDialog.test.tsx.)
    expect(screen.queryByTestId("add-agent-dialog")).toBeNull();
  });

  const AGENT_KIND_CASES: Array<{
    name: string;
    labels: Record<string, string>;
    agentName?: string | null;
    expectedKind: string;
  }> = [
    {
      name: "claude-native wrapper → claude-native marker",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      expectedKind: "claude-native",
    },
    {
      name: "codex-native wrapper → codex-native marker",
      labels: { "omnigent.wrapper": "codex-native-ui" },
      expectedKind: "codex-native",
    },
    {
      name: "opencode-native wrapper → opencode-native marker",
      labels: { "omnigent.wrapper": "opencode-native-ui" },
      expectedKind: "opencode-native",
    },
    {
      name: "pi-native wrapper → pi-native marker",
      labels: { "omnigent.wrapper": "pi-native-ui" },
      expectedKind: "pi-native",
    },
    {
      name: "nessie agent → nessie marker",
      labels: {},
      agentName: "nessie",
      expectedKind: "nessie",
    },
    {
      name: "no wrapper label → generic agent marker",
      labels: {},
      expectedKind: "agent",
    },
    {
      name: "different wrapper label → generic agent marker",
      labels: { "omnigent.wrapper": "some-other-wrapper" },
      expectedKind: "agent",
    },
  ];

  it.each(AGENT_KIND_CASES)(
    "stamps the main row's data-agent-kind from the parent session's wrapper label / agent name ($name)",
    ({ labels, agentName, expectedKind }) => {
      // The leading icon on the main row swaps between Claude, nessie,
      // and generic based on the parent session's `omnigent.wrapper`
      // label (claude-native) and `agentName` (nessie). We assert via
      // the row's data-agent-kind attribute (set alongside the icon
      // swap) so the test isn't coupled to the SVG internals.
      //
      // Failure of this assertion would mean either the wrapper-
      // label probe drifted from the canonical "claude-code-native-ui"
      // value (matched in Sidebar.tsx and TerminalFirstContext.tsx),
      // or the icon-swap branch on `MainRow` regressed.
      useChildSessionsMock.mockReturnValue({
        children: [],
        isLoading: false,
        error: null,
      });
      useSessionMock.mockReturnValue({
        session: {
          id: "conv_root",
          agentId: "ag_root",
          agentName: agentName ?? null,
          runnerId: null,
          status: "idle",
          createdAt: 0,
          title: null,
          labels,
          items: [],
          pendingElicitations: [],
          permissionLevel: 4,
          parentSessionId: null,
          subAgentName: null,
        },
        isLoading: false,
        error: null,
      });

      renderPanel({ rootSessionId: "conv_root" });

      const row = screen.getByTestId("subagent-main-row");
      expect(row).toHaveAttribute("data-agent-kind", expectedKind);
    },
  );

  it("renders a loading state when the initial fetch is in flight", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: true, error: null });

    renderPanel();

    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
  });

  it("renders an error state when the fetch fails with no cached data", () => {
    useChildSessionsMock.mockReturnValue({
      children: [],
      isLoading: false,
      error: new Error("network down"),
    });

    renderPanel();

    expect(screen.getByText(/Failed to load agents/i)).toBeInTheDocument();
  });

  it("renders one row per child session below 'main' and links to each", () => {
    mockChildTree({
      conv_parent: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "frontend_engineer:rail",
          tool: "frontend_engineer",
          session_name: "rail",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: "Inspecting the rail layout",
          pending_elicitations_count: 0,
        },
      ],
    });

    renderPanel();

    // main row first.
    expect(screen.getByTestId("subagent-main-row")).toBeInTheDocument();
    const rows = screen.getAllByTestId("subagent-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute("href", "/c/conv_child_a");
    expect(rows[1]).toHaveAttribute("href", "/c/conv_child_b");
    // Status-word display (which states show a word vs. a bare dot) is owned
    // by the dedicated "shows the status word only for notable states" test.
  });

  it("uses spawned task titles as the primary child-row labels", () => {
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_child",
          title: "codex:auth-refactor",
          tool: "codex",
          session_name: "auth-refactor",
        }),
        childInfo({
          id: "conv_title_only",
          title: "codex:fix-sse-error",
          tool: "codex",
          session_name: null,
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    const row = childRow(container, "conv_child");
    expect(within(row).getByText("auth-refactor")).toBeInTheDocument();
    expect(within(row).queryByText("codex")).toBeNull();
    const titleOnlyRow = childRow(container, "conv_title_only");
    expect(within(titleOnlyRow).getByText("fix-sse-error")).toBeInTheDocument();
    expect(within(titleOnlyRow).queryByText("codex")).toBeNull();
  });

  it("keeps native Codex collaborator rows labeled by nickname instead of thread id", () => {
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_child",
          title: "codex-native-ui-subagent:thread_child_alpha",
          tool: "auth-auditor",
          session_name: "thread_child_alpha",
          labels: { "omnigent.wrapper": "codex-native-ui-subagent" },
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    const row = childRow(container, "conv_child");
    expect(within(row).getByText("auth-auditor")).toBeInTheDocument();
    expect(within(row).queryByText("thread_child_alpha")).toBeNull();
  });

  it("uses native logos for Claude Code, Codex, OpenCode, and Kiro child rows", () => {
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_codex",
          title: "codex:auth-refactor",
          tool: "codex",
          session_name: "auth-refactor",
          labels: { "omnigent.wrapper": "codex-native-ui" },
        }),
        childInfo({
          id: "conv_opencode",
          title: "opencode:port-auth-refactor",
          tool: "opencode",
          session_name: "port-auth-refactor",
          labels: { "omnigent.wrapper": "opencode-native-ui" },
        }),
        childInfo({
          id: "conv_claude",
          title: "claude_code:review-auth-refactor",
          tool: "claude_code",
          session_name: "review-auth-refactor",
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        }),
        childInfo({
          id: "conv_kiro",
          title: "kiro:harden-auth",
          tool: "kiro",
          session_name: "harden-auth",
          labels: { "omnigent.wrapper": "kiro-native-ui" },
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    const codexRow = childRow(container, "conv_codex");
    expect(codexRow.querySelector('[data-icon="codex"]')).not.toBeNull();
    expect(codexRow.querySelector(".lucide-code-2")).toBeNull();

    const opencodeRow = childRow(container, "conv_opencode");
    expect(opencodeRow.querySelector('[data-icon="opencode"]')).not.toBeNull();
    expect(opencodeRow.querySelector(".lucide-code-2")).toBeNull();

    const claudeRow = childRow(container, "conv_claude");
    expect(claudeRow.querySelector('[data-icon="claude"]')).not.toBeNull();
    expect(claudeRow.querySelector(".lucide-code-2")).toBeNull();

    // Kiro renders its own glyph now, not the borrowed Cursor mark (#1137).
    const kiroRow = childRow(container, "conv_kiro");
    expect(kiroRow.querySelector('[data-icon="kiro"]')).not.toBeNull();
  });

  it("gives native sub-agent children role/Otto icons, not the brand logo", () => {
    // A native session's sub-agents are all the same brand, so the logo
    // is reserved for full native sessions; sub-agent rows read by role,
    // with the Otto mascot as the generic fallback.
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_generic",
          title: "claude:tell-a-joke",
          tool: "claude",
          labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
        }),
        childInfo({
          id: "conv_explore",
          title: "Explore:find-the-bug",
          tool: "Explore",
          labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    const genericRow = childRow(container, "conv_generic");
    expect(genericRow.querySelector('[data-icon="otto"]')).not.toBeNull();
    expect(genericRow.querySelector('[data-icon="claude"]')).toBeNull();

    const exploreRow = childRow(container, "conv_explore");
    expect(exploreRow.querySelector(".lucide-search")).not.toBeNull();
    expect(exploreRow.querySelector('[data-icon="claude"]')).toBeNull();
  });

  it("does not infer native logos from child title or tool names alone", () => {
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_custom",
          title: "codex:custom-review",
          tool: "codex",
          session_name: "custom-review",
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    const row = childRow(container, "conv_custom");
    expect(row.querySelector('[data-icon="codex"]')).toBeNull();
    expect(row.querySelector("svg:not([data-icon]):not(.lucide-corner-down-right)")).not.toBeNull();
  });

  it("uses the pi glyph for pi child rows, matched by exact tool name", () => {
    mockChildTree({
      conv_root: [
        // Scaffold pi worker: no wrapper label by design, so the spawn
        // title's agent-type head ("pi", surfaced as tool) is the signal.
        childInfo({
          id: "conv_pi",
          title: "pi:review-auth",
          tool: "pi",
          session_name: "review-auth",
        }),
        // Near-miss agent name containing "pi" — must stay generic.
        childInfo({
          id: "conv_pipeline",
          title: "pipeline:build",
          tool: "pipeline",
          session_name: "build",
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    // The pi glyph proves the row matched the scaffold child by agent name;
    // a generic bot icon here means the pi branch was dropped or mis-keyed.
    const piRow = childRow(container, "conv_pi");
    expect(piRow.querySelector('[data-icon="pi"]')).not.toBeNull();

    // A substring match (e.g. tool.includes("pi")) would wrongly brand this
    // row; it must fall back to the generic Otto icon.
    const pipelineRow = childRow(container, "conv_pipeline");
    expect(pipelineRow.querySelector('[data-icon="pi"]')).toBeNull();
    expect(pipelineRow.querySelector('[data-icon="otto"]')).not.toBeNull();
  });

  it("native wrapper labels outrank the pi tool-name match", () => {
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_native_pi",
          title: "pi:port-fix",
          tool: "pi",
          session_name: "port-fix",
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        }),
      ],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    // The wrapper label is authoritative identity: a native child keeps its
    // native glyph even when its tool name collides with "pi".
    const row = childRow(container, "conv_native_pi");
    expect(row.querySelector('[data-icon="claude"]')).not.toBeNull();
    expect(row.querySelector('[data-icon="pi"]')).toBeNull();
  });

  it("fetches child sessions without polling (push-driven via watch-set)", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    renderPanel({ rootSessionId: "conv_root" });

    expect(useChildSessionsMock).toHaveBeenCalledWith("conv_root");
    expect(useChildSessionsMock).not.toHaveBeenCalledWith("conv_root", expect.any(Number));
  });

  it("highlights the main row when conversationId === rootSessionId (on the parent)", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    renderPanel({ conversationId: "conv_root", rootSessionId: "conv_root" });

    // Split-and-contain rather than regex so we don't false-match
    // ``hover:bg-accent/60`` on the inactive base style.
    expect(screen.getByTestId("subagent-main-row").className.split(/\s+/)).toContain("bg-accent");
  });

  it("highlights the matching child row when viewing a sibling", () => {
    mockChildTree({
      conv_root: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "frontend_engineer:rail",
          tool: "frontend_engineer",
          session_name: "rail",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
    });

    // User is viewing child_a; the rail still keys off the root so
    // we see both siblings, with child_a highlighted.
    renderPanel({ conversationId: "conv_child_a", rootSessionId: "conv_root" });

    expect(screen.getByTestId("subagent-main-row").className.split(/\s+/)).not.toContain(
      "bg-accent",
    );
    const rows = screen.getAllByTestId("subagent-row");
    expect(rows[0].className.split(/\s+/)).toContain("bg-accent");
    expect(rows[1].className.split(/\s+/)).not.toContain("bg-accent");
  });

  it("renders 'Failed' for a child whose latest task ended in failure", () => {
    // Failed keeps the label in destructive text, with a trailing dot
    // matching the rest of the rail's compact status markers.
    mockChildTree({
      conv_parent: [
        {
          id: "conv_child",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "failed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
    });

    renderPanel();

    const row = screen.getByTestId("subagent-row");
    expect(row).toHaveTextContent(/Failed/);
    // Should not lowercase-bleed the raw status into the label.
    expect(row.textContent).not.toMatch(/failed[^F]/);
  });

  it("renders a child with last_task_error as failed and exposes the error detail", () => {
    mockChildTree({
      conv_parent: [
        childInfo({
          id: "conv_child",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          last_task_error: {
            code: "required_terminal_exited",
            message:
              "\nRequired terminal exited unexpectedly; the session runtime is no longer available.\ncommand: worker-cli",
          },
        }),
      ],
    });

    const { container } = renderPanel();

    const row = childRow(container, "conv_child");
    const indicator = within(row).getByTestId("subagent-status-dot");
    expect(row).toHaveTextContent(/Failed/);
    expect(indicator).toHaveAttribute(
      "aria-label",
      "Failed: Required terminal exited unexpectedly; the session runtime is no longer available.",
    );
  });

  it.each([
    ["runner_disconnected", "Runner disconnected unexpectedly."],
    ["runner_failed_to_start", "runner exited before the first turn"],
  ])(
    "renders a quiet grey disconnected dot (no word, not red 'Failed') for the runner-disconnect code %s",
    (code, message) => {
      // Option B: a runner that merely disconnected/exited is NOT a task
      // failure. The child summary still collapses to current_task_status
      // "failed", but last_task_error.code carries the disconnect cause, so
      // the row must branch to the quiet disconnected dot before the red
      // "Failed" pill. The dot shows NO inline word — the cause lives in the
      // tooltip — and uses the neutral grey --muted-foreground token, never
      // the shared amber --warning.
      mockChildTree({
        conv_parent: [
          childInfo({
            id: "conv_child",
            title: "researcher:auth",
            tool: "researcher",
            session_name: "auth",
            current_task_status: "failed",
            last_task_error: { code, message },
          }),
        ],
      });

      const { container } = renderPanel();

      const row = childRow(container, "conv_child");
      const indicator = within(row).getByTestId("subagent-status-dot");
      // The inline "Disconnected" word is hidden (quiet dot, like idle/done).
      expect(row).not.toHaveTextContent(/Disconnected/);
      expect(row).not.toHaveTextContent(/Failed/);
      // Positive quiet-dot guarantee: disconnected falls through to the
      // generic quiet-dot path, so the wrapper carries the standard muted text
      // class (same as idle/done) and the grey --muted-foreground dot is the
      // ONLY color hook — no warning/destructive bleed on the wrapper or the
      // dot, and crucially never the shared amber --warning (owned by "Needs
      // response"). The blue --session-active hue is now reserved for the
      // launching/idle/done dots, so it must NOT appear here.
      expect(indicator).toHaveClass("text-muted-foreground");
      expect(indicator).not.toHaveClass("text-warning");
      expect(indicator).not.toHaveClass("text-destructive");
      const dot = indicator.querySelector("span.rounded-full");
      expect(dot).toHaveClass("bg-muted-foreground");
      expect(dot).not.toHaveClass("bg-session-active");
      expect(dot).not.toHaveClass("bg-warning");
      expect(dot).not.toHaveClass("bg-destructive");
      // The cause is still surfaced in the accessible label / tooltip.
      expect(indicator).toHaveAttribute("aria-label", `Disconnected: ${message}`);
    },
  );

  it("keeps rendering red 'Failed' for a genuine task failure (non-disconnect code)", () => {
    // Guard the hard constraint: only runner-disconnect/exit codes become
    // "Disconnected"; any other error code is a real failure and stays red.
    mockChildTree({
      conv_parent: [
        childInfo({
          id: "conv_child",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "failed",
          last_task_error: { code: "tool_error", message: "Tool raised ValueError" },
        }),
      ],
    });

    const { container } = renderPanel();

    const row = childRow(container, "conv_child");
    const indicator = within(row).getByTestId("subagent-status-dot");
    expect(row).toHaveTextContent(/Failed/);
    expect(row).not.toHaveTextContent(/Disconnected/);
    expect(indicator).toHaveClass("text-destructive");
    expect(indicator.querySelector(".bg-destructive")).not.toBeNull();
  });

  it.each([
    ["runner_disconnected", "Runner disconnected unexpectedly."],
    ["runner_failed_to_start", "runner exited before the first turn"],
  ])(
    "renders a quiet grey disconnected dot on the main row when the parent failed with the runner-disconnect code %s",
    (code, message) => {
      // The session snapshot collapses a runner exit to status "failed" but
      // preserves the cause in lastTaskError.code (runner_failed_to_start /
      // runner_disconnected). The main row must branch on it to render the
      // quiet grey disconnected dot rather than the red "Failed" pill.
      // Parametrized over BOTH disconnect codes, mirroring the child-row
      // it.each above so neither code can regress on the main row.
      useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
      useSessionMock.mockReturnValue({
        session: {
          id: "conv_root",
          agentId: "ag_root",
          agentName: null,
          runnerId: null,
          status: "failed",
          lastTaskError: { code, message },
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
      } as unknown as ReturnType<typeof useSession>);

      renderPanel({ rootSessionId: "conv_root" });

      const mainRow = screen.getByTestId("subagent-main-row");
      const indicator = within(mainRow).getByTestId("subagent-status-dot");
      // Quiet grey dot, no inline word — distinct from the red "Failed" pill.
      expect(mainRow).not.toHaveTextContent(/Disconnected/);
      expect(mainRow).not.toHaveTextContent(/Failed/);
      // Positive quiet-dot guarantee: the wrapper carries the standard muted
      // quiet-dot text class (same path as idle/done) and the grey dot is the
      // ONLY color hook — no warning/destructive class bleeds onto either the
      // wrapper or the dot. The blue --session-active hue is reserved for the
      // launching/idle/done dots, so it must NOT appear here.
      expect(indicator).toHaveClass("text-muted-foreground");
      expect(indicator).not.toHaveClass("text-warning");
      expect(indicator).not.toHaveClass("text-destructive");
      const dot = indicator.querySelector("span.rounded-full");
      expect(dot).toHaveClass("bg-muted-foreground");
      expect(dot).not.toHaveClass("bg-session-active");
      expect(dot).not.toHaveClass("bg-warning");
      expect(dot).not.toHaveClass("bg-destructive");
      // The disconnect cause stays in the tooltip / accessible label.
      expect(indicator).toHaveAttribute("aria-label", `Disconnected: ${message}`);
    },
  );

  it("renders red 'Failed' on the main row for a genuine parent failure (non-disconnect code)", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: null,
        runnerId: null,
        status: "failed",
        lastTaskError: { code: "turn_error", message: "turn setup failed" },
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
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({ rootSessionId: "conv_root" });

    const mainRow = screen.getByTestId("subagent-main-row");
    const indicator = within(mainRow).getByTestId("subagent-status-dot");
    expect(mainRow).toHaveTextContent(/Failed/);
    expect(mainRow).not.toHaveTextContent(/Disconnected/);
    expect(indicator).toHaveClass("text-destructive");
  });

  it("shows the pulsing working dot (no redundant 'Working' word) on the main row when the parent session is running", () => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: null,
        runnerId: null,
        status: "running",
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
    });

    renderPanel({ rootSessionId: "conv_root" });

    const mainRow = screen.getByTestId("subagent-main-row");
    // The pulsing pink dot reads as "active" on its own, so the word is dropped.
    expect(mainRow.querySelector('[data-testid="running-dot"]')).not.toBeNull();
    expect(mainRow).not.toHaveTextContent(/Working/i);
    // The label survives as the accessible name on the status indicator.
    expect(within(mainRow).getByTestId("subagent-status-dot")).toHaveAttribute(
      "aria-label",
      "Working",
    );
  });

  it("renders the last_message_preview underneath the title when present", () => {
    mockChildTree({
      conv_parent: [
        {
          id: "conv_child",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: "Searching the codebase for references to the old API…",
          pending_elicitations_count: 0,
        },
      ],
    });

    renderPanel();

    expect(
      screen.getByText("Searching the codebase for references to the old API…"),
    ).toBeInTheDocument();
  });

  it("tokenizes status colors and drops the raw stoplight Tailwind classes", () => {
    mockChildTree({
      conv_parent: [
        childInfo({
          id: "c_work",
          tool: "researcher",
          busy: true,
          current_task_status: "in_progress",
        }),
        childInfo({ id: "c_launch", tool: "researcher", current_task_status: "launching" }),
        childInfo({ id: "c_done", tool: "researcher", current_task_status: "completed" }),
        childInfo({ id: "c_idle", tool: "researcher" }),
        // A verbatim "other status" fallthrough — the GREY exception.
        childInfo({ id: "c_other", tool: "researcher", current_task_status: "cancelled" }),
        childInfo({ id: "c_fail", tool: "researcher", current_task_status: "failed" }),
      ],
    });

    const { container } = renderPanel();

    const dotOf = (id: string) =>
      within(childRow(container, id))
        .getByTestId("subagent-status-dot")
        .querySelector("span.rounded-full");

    // Working reuses the sidebar RunningDot in the grey tone —
    // identical to the sidebar's running indicator; a wrong tone drops
    // text-muted-foreground.
    expect(
      childRow(container, "c_work").querySelector(
        '[data-testid="running-dot"].text-muted-foreground',
      ),
    ).not.toBeNull();

    // Panel-scoped palette swap: the quiet live/settled states read in the
    // blue --session-active hue, each PRESERVING its prior opacity treatment
    // (launching kept /70, idle + done kept /55) — only the hue flipped from
    // grey to blue.
    const launchDot = dotOf("c_launch");
    expect(launchDot).toHaveClass("bg-session-active/70");
    expect(launchDot).not.toHaveClass("bg-muted-foreground/70");
    const doneDot = dotOf("c_done");
    expect(doneDot).toHaveClass("bg-session-active/55");
    expect(doneDot).not.toHaveClass("bg-muted-foreground/55");
    const idleDot = dotOf("c_idle");
    expect(idleDot).toHaveClass("bg-session-active/55");
    expect(idleDot).not.toHaveClass("bg-muted-foreground/55");

    // Exception: the verbatim "other status" fallthrough STAYS neutral grey —
    // it is the one quiet dot the swap deliberately leaves on --muted-foreground.
    const otherDot = dotOf("c_other");
    expect(otherDot).toHaveClass("bg-muted-foreground/55");
    expect(otherDot).not.toHaveClass("bg-session-active/55");

    // Terminal failures keep destructive text + a destructive dot.
    const failedIndicator = within(childRow(container, "c_fail")).getByTestId(
      "subagent-status-dot",
    );
    expect(failedIndicator).toHaveClass("text-destructive");
    expect(failedIndicator.querySelector(".bg-destructive")).not.toBeNull();
    expect(failedIndicator.querySelector("svg")).toBeNull();
    // Regression guard: "done" must not regress to the loud green success tone.
    expect(childRow(container, "c_done").querySelector(".bg-success")).toBeNull();
    // Regression guard: the pre-polish stoplight classes must be gone.
    expect(container.querySelector(".bg-amber-500")).toBeNull();
    expect(container.querySelector(".bg-emerald-500")).toBeNull();
    expect(container.querySelector(".bg-red-500")).toBeNull();
  });

  it("surfaces an 'awaiting input' badge for a child parked on an elicitation", () => {
    mockChildTree({
      conv_parent: [
        // Parked on a prompt while its turn is still live (busy=true).
        // Awaiting must outrank busy so the user sees it needs input.
        childInfo({
          id: "c_await",
          tool: "researcher",
          busy: true,
          pending_elicitations_count: 1,
        }),
      ],
    });

    const { container } = renderPanel();

    const row = childRow(container, "c_await");
    // The awaiting state renders the "Needs response" tag (mirroring the
    // sidebar) — if this reads "Working", the awaiting check isn't
    // outranking busy and the signal is hidden.
    expect(row).toHaveTextContent(/Needs response/);
    expect(within(row).getByTestId("subagent-status-dot")).toHaveAttribute(
      "aria-label",
      "Needs response",
    );
    // The working RunningDot must NOT render for an awaiting child —
    // confirms the elicitation branch replaced the busy branch.
    expect(row.querySelector('[data-testid="running-dot"]')).toBeNull();
  });

  it("shows the status word only for notable states, not quiet ones", () => {
    mockChildTree({
      conv_parent: [
        childInfo({ id: "c_launch", tool: "researcher", current_task_status: "launching" }),
        childInfo({ id: "c_work", tool: "researcher", busy: true }),
        childInfo({ id: "c_done", tool: "researcher", current_task_status: "completed" }),
        childInfo({ id: "c_idle", tool: "researcher" }),
        childInfo({ id: "c_cancel", tool: "researcher", current_task_status: "cancelled" }),
      ],
    });

    const { container } = renderPanel();

    // The unexpected "cancelled" terminal state keeps its word so it stands out.
    expect(childRow(container, "c_cancel")).toHaveTextContent(/cancelled/);
    // Launching is not yet real work, so it shows its word and does not reuse
    // the active running dot. Its inline word now reads in the blue
    // --session-active hue (matching its dot), not the neutral muted text.
    expect(childRow(container, "c_launch")).toHaveTextContent(/Launching/);
    expect(childRow(container, "c_launch").querySelector('[data-testid="running-dot"]')).toBeNull();
    const launchIndicator = within(childRow(container, "c_launch")).getByTestId(
      "subagent-status-dot",
    );
    expect(launchIndicator).toHaveClass("text-session-active");
    expect(launchIndicator).not.toHaveClass("text-muted-foreground");
    // The verbatim "other" word stays neutral grey — its wrapper keeps the
    // muted text class (the GREY exception).
    const cancelIndicator = within(childRow(container, "c_cancel")).getByTestId(
      "subagent-status-dot",
    );
    expect(cancelIndicator).toHaveClass("text-muted-foreground");
    expect(cancelIndicator).not.toHaveClass("text-session-active");
    // Quiet states render no word — the label lives in the tooltip. Working is
    // quiet too: the pulsing pink dot already reads as "active".
    expect(childRow(container, "c_work")).not.toHaveTextContent(/Working/);
    expect(childRow(container, "c_done")).not.toHaveTextContent(/Done/);
    expect(childRow(container, "c_idle")).not.toHaveTextContent(/Idle/);
    // The working dot still renders, and the word survives as the dot's label.
    expect(
      childRow(container, "c_work").querySelector('[data-testid="running-dot"]'),
    ).not.toBeNull();
    expect(
      within(childRow(container, "c_work")).getByTestId("subagent-status-dot"),
    ).toHaveAttribute("aria-label", "Working");
    expect(
      within(childRow(container, "c_idle")).getByTestId("subagent-status-dot"),
    ).toHaveAttribute("aria-label", "Idle");
  });

  it("renders the status marker last so markers align across rows regardless of label width", () => {
    // Alignment fix: the marker trails the label, so a wide label ("Failed")
    // can't push its marker left of a bare "Idle" dot. If the marker ever
    // renders before the label again, lastElementChild stops being the marker
    // and the markers fall out of column.
    mockChildTree({
      conv_parent: [
        childInfo({ id: "c_fail", tool: "researcher", current_task_status: "failed" }),
        childInfo({ id: "c_idle", tool: "researcher" }),
      ],
    });

    const { container } = renderPanel();

    // Failed row shows its label, with the destructive dot trailing it.
    const failIndicator = within(childRow(container, "c_fail")).getByTestId("subagent-status-dot");
    const failDot = failIndicator.querySelector("span.rounded-full");
    expect(failDot).not.toBeNull();
    expect(failIndicator).toHaveTextContent(/Failed/);
    expect(failIndicator.lastElementChild).toBe(failDot);

    // Idle row is dot-only (quiet), and that dot is still the trailing child.
    const idleIndicator = within(childRow(container, "c_idle")).getByTestId("subagent-status-dot");
    const idleDot = idleIndicator.querySelector("span.rounded-full");
    expect(idleDot).not.toBeNull();
    expect(idleIndicator.lastElementChild).toBe(idleDot);
  });

  it("de-emphasizes settled (done/idle) rows, but never the working or active row", () => {
    mockChildTree({
      conv_root: [
        childInfo({ id: "c_work", tool: "researcher", busy: true }),
        childInfo({ id: "c_done", tool: "researcher", current_task_status: "completed" }),
        childInfo({ id: "c_idle", tool: "researcher" }),
      ],
    });

    // Viewing c_done makes it the active row; active must win over dimming.
    const { container } = renderPanel({ conversationId: "c_done", rootSessionId: "conv_root" });

    // Working stays full-strength so it dominates the list.
    expect(childRow(container, "c_work").className.split(/\s+/)).not.toContain("opacity-60");
    // Settled-and-not-active row is dimmed.
    expect(childRow(container, "c_idle").className.split(/\s+/)).toContain("opacity-60");
    // Settled row you're viewing is not dimmed (active beats dim).
    expect(childRow(container, "c_done").className.split(/\s+/)).not.toContain("opacity-60");
  });

  it("renders a distinct, role-specific icon per agent type", () => {
    mockChildTree({
      conv_parent: [
        childInfo({ id: "c_explore", tool: "Explore", busy: true }),
        childInfo({ id: "c_code", tool: "frontend_engineer", busy: true }),
      ],
    });

    const { container } = renderPanel();

    // The "all Explore look alike" fix: distinct roles get distinct glyphs,
    // and the icon is actually wired into the row. (Exact type→icon mapping
    // is verified by the it.each unit test below.) Comparing class inequality
    // avoids hardcoding lucide's internal names; if the map regressed to one
    // icon for all types, the two classes would be equal and this would fail.
    // Skip the decorative nesting connector (shared by every row) and assert
    // on the role icon, which is what varies per agent type.
    const exploreIcon = childRow(container, "c_explore").querySelector(
      "svg:not(.lucide-corner-down-right)",
    );
    const codeIcon = childRow(container, "c_code").querySelector(
      "svg:not(.lucide-corner-down-right)",
    );
    expect(exploreIcon).not.toBeNull();
    expect(codeIcon).not.toBeNull();
    expect(exploreIcon?.getAttribute("class")).not.toEqual(codeIcon?.getAttribute("class"));
  });

  it("strips session-scoped search params from rail navigation hrefs", () => {
    // Regression guard for the bug where clicking a sub-agent row
    // preserved the previous session's ``?file=…`` query param. The
    // conversation-switch effect in AppShell would then re-open the
    // file viewer for that path against the new session — yanking
    // the user into the file rail unexpectedly.
    //
    // The fix strips ``file``/``diff``/``comment``/``view`` on rail
    // Links (both ``main`` and child rows). We assert via the
    // rendered href: react-router-dom resolves ``<Link to={{
    // pathname, search }}>`` to ``<a href="pathname?search">``, so a
    // stale session param must not appear in the href.
    mockChildTree({
      conv_root: [
        childInfo({
          id: "conv_child_a",
          tool: "researcher",
          session_name: "auth",
        }),
      ],
    });

    renderPanel({
      rootSessionId: "conv_root",
      // Simulate stale session-scoped params carried over from the
      // previous session — the bug condition the fix targets. All four
      // are listed so any regression that drops a key from
      // SESSION_SCOPED_PARAMS surfaces here.
      initialEntries: ["/c/conv_root?file=existing.txt&diff=1&comment=c1&view=changed"],
    });

    const main = screen.getByTestId("subagent-main-row");
    expect(main).toHaveAttribute("href", "/c/conv_root");
    expect(main.getAttribute("href")).not.toContain("?");

    const child = screen.getByTestId("subagent-row");
    expect(child).toHaveAttribute("href", "/c/conv_child_a");
    expect(child.getAttribute("href")).not.toContain("?");
  });

  it("preserves global search params (?debug=1) across rail navigation", () => {
    // Regression guard: ``?debug=1`` powers ``useDebugMode()`` which
    // reveals execution-log entries in the rail. The earlier fix
    // cleared all search params on rail navigation, which silently
    // turned debug mode off when switching agents. Only the
    // session-scoped params should be stripped — anything else
    // (debug, future global flags) must survive.
    mockChildTree({
      conv_root: [childInfo({ id: "conv_child_a", tool: "researcher" })],
    });

    renderPanel({
      rootSessionId: "conv_root",
      initialEntries: ["/c/conv_root?file=foo.txt&debug=1"],
    });

    // Both rows must keep ``?debug=1`` while dropping ``file=``.
    const main = screen.getByTestId("subagent-main-row");
    expect(main.getAttribute("href")).toBe("/c/conv_root?debug=1");

    const child = screen.getByTestId("subagent-row");
    expect(child.getAttribute("href")).toBe("/c/conv_child_a?debug=1");
  });

  it.each(ICON_CASES)("maps agent type %s to its category icon", (tool, expected) => {
    // Substring precedence matters: "code-reviewer" must hit review before
    // code, "pr-test-analyzer" must hit test — a wrong branch order regresses.
    expect(iconForAgentType(tool)).toBe(expected);
  });

  it("shows instance names for user-added and normal spawned sub-agents", () => {
    // A user-added agent ("ui:<agent>:<name>" title) should display the name
    // the user typed (e.g. "jimmy"), NOT the agent type — the user named this
    // instance and the icon already conveys the type. Normal LLM-spawned
    // sub-agents use the same display rule: show the explicit task label, not
    // the raw sub-agent type. Regression guard for the reported bugs where
    // rows showed "claude-native-ui", "codex", or "researcher" instead of the
    // meaningful instance label.
    mockChildTree({
      conv_parent: [
        childInfo({
          id: "conv_added",
          title: "ui:claude-native-ui:jimmy",
          tool: "claude-native-ui",
          session_name: "jimmy",
        }),
        childInfo({
          id: "conv_llm",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
        }),
      ],
    });

    const { container } = renderPanel();

    const added = childRow(container, "conv_added");
    expect(added).toHaveTextContent("jimmy");
    expect(added).not.toHaveTextContent("claude-native-ui");
    const llmSpawned = childRow(container, "conv_llm");
    expect(llmSpawned).toHaveTextContent("auth");
    expect(llmSpawned).not.toHaveTextContent("researcher");
  });

  it("renders grandchildren and deeper levels indented under their parents", () => {
    mockChildTree({
      conv_root: [childInfo({ id: "conv_child", tool: "researcher", session_name: "auth" })],
      conv_child: [childInfo({ id: "conv_grandchild", tool: "Explore", session_name: "files" })],
      conv_grandchild: [childInfo({ id: "conv_ggchild", tool: "Explore", session_name: "deep" })],
    });

    const { container } = renderPanel({ rootSessionId: "conv_root" });

    // Depth-first order: each child is followed by its own subtree.
    const rows = screen.getAllByTestId("subagent-row");
    expect(rows.map((r) => r.getAttribute("data-child-session-id"))).toEqual([
      "conv_child",
      "conv_grandchild",
      "conv_ggchild",
    ]);
    expect(childRow(container, "conv_child")).toHaveAttribute("data-depth", "1");
    expect(childRow(container, "conv_grandchild")).toHaveAttribute("data-depth", "2");
    expect(childRow(container, "conv_ggchild")).toHaveAttribute("data-depth", "3");
    // Each level steps its left gutter in by one unit so the rows read
    // as a tree.
    const pad = (id: string) => parseInt(childRow(container, id).style.paddingLeft, 10);
    expect(pad("conv_grandchild")).toBeGreaterThan(pad("conv_child"));
    expect(pad("conv_ggchild")).toBeGreaterThan(pad("conv_grandchild"));
  });

  it("stops fetching and rendering below the depth cap", () => {
    mockChildTree({
      conv_root: [childInfo({ id: "c1", tool: "researcher" })],
      c1: [childInfo({ id: "c2", tool: "researcher" })],
      c2: [childInfo({ id: "c3", tool: "researcher" })],
      // Beyond the cap: must never be fetched or rendered.
      c3: [childInfo({ id: "c4", tool: "researcher" })],
    });

    renderPanel({ rootSessionId: "conv_root" });

    expect(
      screen.getAllByTestId("subagent-row").map((r) => r.getAttribute("data-child-session-id")),
    ).toEqual(["c1", "c2", "c3"]);
    // The depth-3 row's child query is disabled (null id) instead of
    // fetching c3's children.
    expect(useChildSessionsMock).toHaveBeenCalledWith(null);
    expect(useChildSessionsMock).not.toHaveBeenCalledWith("c3");
  });

  it("highlights the active grandchild row", () => {
    mockChildTree({
      conv_root: [childInfo({ id: "conv_child", tool: "researcher" })],
      conv_child: [childInfo({ id: "conv_grandchild", tool: "Explore" })],
    });

    const { container } = renderPanel({
      conversationId: "conv_grandchild",
      rootSessionId: "conv_root",
    });

    expect(childRow(container, "conv_grandchild").className.split(/\s+/)).toContain("bg-accent");
    expect(childRow(container, "conv_child").className.split(/\s+/)).not.toContain("bg-accent");
  });
});
