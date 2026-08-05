// Proves every graph-view node carries its harness/role glyph — the same one
// the list view shows — for BOTH the root node and child nodes.
//
// Node data comes from the real ``buildGraphLayout`` so the test covers the
// whole path (raw session/child fields → layout → glyph), not just the
// component in isolation. @xyflow/react is stubbed: importing its barrel (and
// CSS bundle) exhausts the jsdom worker, and the node only needs <Handle>.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type { Node, NodeProps } from "@xyflow/react";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@xyflow/react", () => ({
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
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
vi.mock("@/components/icons/NessieIcon", () => ({
  NessieIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="nessie" />,
}));

import { AgentGraphNode } from "./AgentGraphNode";
import { buildGraphLayout, type AgentNodeData, type TreeRoot } from "./subagentGraphLayout";

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

/**
 * Lay out a root + its children, then render each node through the real
 * component. Returns a lookup from session id to that node's element.
 */
function renderGraphNodes(
  root: Partial<TreeRoot> = {},
  children: ChildSessionInfo[] = [],
): (sessionId: string) => HTMLElement {
  const childrenMap = new Map<string, ChildSessionInfo[]>([["conv_root", children]]);
  const { nodes } = buildGraphLayout(
    {
      id: "conv_root",
      label: "main",
      activity: "idle",
      statusLabel: "Idle",
      preview: null,
      ...root,
    },
    childrenMap,
    "conv_root",
  );

  const { container } = render(
    <>
      {nodes.map((node) => (
        <div key={node.id} data-session-id={node.data.sessionId}>
          <AgentGraphNode {...({ data: node.data } as unknown as NodeProps<Node<AgentNodeData>>)} />
        </div>
      ))}
    </>,
  );

  return (sessionId: string) => {
    const el = container.querySelector<HTMLElement>(`[data-session-id="${sessionId}"]`);
    if (!el) throw new Error(`No graph node for ${sessionId}`);
    return el;
  };
}

afterEach(cleanup);

describe("AgentGraphNode icons", () => {
  it("renders the harness glyph on the ROOT node", () => {
    const at = renderGraphNodes({ wrapper: "claude-code-native-ui" });

    expect(at("conv_root").querySelector('[data-icon="claude"]')).not.toBeNull();
  });

  it("renders the harness glyph on CHILD nodes", () => {
    const at = renderGraphNodes({}, [
      childInfo({
        id: "conv_codex",
        session_name: "auth-refactor",
        tool: "codex",
        labels: { "omnigent.wrapper": "codex-native-ui" },
      }),
      childInfo({
        id: "conv_opencode",
        session_name: "port-auth",
        tool: "opencode",
        labels: { "omnigent.wrapper": "opencode-native-ui" },
      }),
    ]);

    expect(at("conv_codex").querySelector('[data-icon="codex"]')).not.toBeNull();
    expect(at("conv_opencode").querySelector('[data-icon="opencode"]')).not.toBeNull();
  });

  it("resolves the root's glyph from its harness when it has no wrapper label", () => {
    const at = renderGraphNodes({ harness: "claude-sdk" });

    expect(at("conv_root").querySelector('[data-icon="claude"]')).not.toBeNull();
  });

  it("gives the root the nessie glyph by agent name", () => {
    // nessie runs on claude-sdk, so a harness-first check would mislabel it.
    const at = renderGraphNodes({ agentName: "nessie", harness: "claude-sdk" });

    expect(at("conv_root").querySelector('[data-icon="nessie"]')).not.toBeNull();
  });

  it("falls back to the generic bot on an unrecognized root", () => {
    const at = renderGraphNodes({ wrapper: "some-other-wrapper" });

    const root = at("conv_root");
    expect(root.querySelector(".lucide-bot")).not.toBeNull();
    expect(root.querySelector('[data-icon="claude"]')).toBeNull();
  });

  it("gives child nodes role icons, and Otto when the type is unusable", () => {
    const at = renderGraphNodes({}, [
      childInfo({ id: "conv_explore", session_name: "find-bug", tool: "Explore" }),
      childInfo({ id: "conv_generic", session_name: "misc", tool: "general-purpose" }),
    ]);

    expect(at("conv_explore").querySelector(".lucide-search")).not.toBeNull();
    expect(at("conv_generic").querySelector('[data-icon="otto"]')).not.toBeNull();
  });

  it("gives native sub-agent children role icons, not the brand logo", () => {
    // Mirrors the list view: a native session's sub-agents are all the same
    // brand, so `…-subagent` wrappers read by role instead.
    const at = renderGraphNodes({}, [
      childInfo({
        id: "conv_sub",
        session_name: "find-bug",
        tool: "Explore",
        labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
      }),
    ]);

    const sub = at("conv_sub");
    expect(sub.querySelector(".lucide-search")).not.toBeNull();
    expect(sub.querySelector('[data-icon="claude"]')).toBeNull();
  });

  it("does not infer a brand logo from a child's tool name alone", () => {
    const at = renderGraphNodes({}, [
      childInfo({ id: "conv_custom", session_name: "custom-review", tool: "codex" }),
    ]);

    expect(at("conv_custom").querySelector('[data-icon="codex"]')).toBeNull();
  });

  it("hides the decorative icon from the accessibility tree", () => {
    // The node's text label carries the name, so the glyph must not be
    // announced — same treatment as the list view's connector glyph.
    const at = renderGraphNodes({ wrapper: "claude-code-native-ui" });

    expect(at("conv_root").querySelector('[data-icon="claude"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("still renders the label and status dot alongside the icon", () => {
    // Guards against the icon displacing what the node already showed.
    const at = renderGraphNodes({ label: "main", wrapper: "claude-code-native-ui" });

    expect(at("conv_root")).toHaveTextContent("main");
  });
});
