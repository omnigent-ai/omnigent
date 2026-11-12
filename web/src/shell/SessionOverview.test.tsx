import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useChatStore } from "@/store/chatStore";
import { SessionCost, SessionOverview } from "./SessionOverview";
import type { FileNavigationGuard } from "./FileViewerContext";

vi.mock("@/hooks/useChildSessions", () => ({
  MAX_TREE_DEPTH: 3,
  useChildSessions: vi.fn((sessionId: string | null) => ({
    children:
      sessionId === null
        ? []
        : sessionId === "child 0"
          ? [{ id: "nested", tool: "Nested", own_cost_usd: 0.2 }]
          : sessionId === "nested"
            ? [{ id: "leaf", tool: "Leaf", own_cost_usd: 0.1 }]
            : Array.from({ length: 25 }, (_, index) => ({
                id: `child ${index}`,
                tool: `Agent ${index}`,
                own_cost_usd: index === 0 ? 0 : 0.5,
              })),
    isLoading: false,
    error: null,
  })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: () => ({
    data: { available: true, data: [{ lines_added: 3, lines_removed: 1 }] },
  }),
}));
vi.mock("@/components/AgentInfo", () => ({ ModelUsageBreakdown: () => null }));

beforeEach(() =>
  useChatStore.setState({
    conversationId: "parent",
    sessionCostUsd: null,
    sessionUsageLoading: true,
    sessionUsageByModel: null,
  }),
);

it("shows loading, true absence, zero, and never another session's known cost", () => {
  render(<SessionCost sessionId="parent" />);
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => useChatStore.setState({ sessionUsageLoading: false }));
  expect(screen.getByText("Unavailable")).toBeInTheDocument();
  act(() => useChatStore.setState({ sessionCostUsd: 0 }));
  expect(screen.getByText("$0.00")).toBeInTheDocument();
  act(() => useChatStore.setState({ conversationId: "other", sessionCostUsd: 99 }));
  expect(screen.queryByText("$99.00")).toBeNull();
  expect(screen.getByRole("status")).toBeInTheDocument();
});

it("exposes all children, cumulative tokens and line counts, and guards the actual link URL", () => {
  useChatStore.setState({
    sessionCostUsd: 1,
    sessionUsageLoading: false,
    sessionUsageByModel: {
      model: {
        totalTokens: 0,
        inputTokens: 0,
        outputTokens: 0,
        cacheReadInputTokens: null,
        cacheCreationInputTokens: null,
        totalCostUsd: 1,
      },
    },
  });
  let accept: (() => void) | undefined;
  const guard: FileNavigationGuard = vi.fn((_path, _options, commit) => {
    accept = commit;
  });
  function Location() {
    return (
      <output data-testid="location">
        {useLocation().pathname}
        {useLocation().search}
      </output>
    );
  }
  const dismiss = vi.fn();
  const openAgents = vi.fn();
  const openChanges = vi.fn();
  render(
    <MemoryRouter initialEntries={["/c/parent?debug=1&file=notes.md"]}>
      <SessionOverview
        sessionId="parent"
        canBrowseFiles
        guard={guard}
        onDismiss={dismiss}
        onOpenAgents={openAgents}
        onOpenChanges={openChanges}
      />
      <Location />
    </MemoryRouter>,
  );
  expect(screen.getByText("+3 −1")).toBeInTheDocument();
  expect(screen.getByText("0")).toBeInTheDocument();
  expect(screen.getAllByRole("link")).toHaveLength(5);
  fireEvent.click(screen.getByRole("button", { name: "Show all 25" }));
  expect(screen.getAllByRole("link")).toHaveLength(25);
  const child = screen.getByRole("link", { name: "Agent 0$0.00" });
  expect(child).toHaveAttribute("href", "/c/child%200?debug=1");
  fireEvent.click(child, { ctrlKey: true });
  expect(guard).not.toHaveBeenCalled();
  fireEvent.click(child);
  expect(screen.getByTestId("location")).toHaveTextContent("/c/parent?debug=1&file=notes.md");
  expect(screen.getByRole("region", { name: "Session overview" })).toBeInTheDocument();
  act(() => accept?.());
  expect(screen.getByTestId("location")).toHaveTextContent("/c/child%200?debug=1");
  for (const [name, callback] of [
    ["View all agents", openAgents],
    ["View changes", openChanges],
  ] as const) {
    fireEvent.click(screen.getByRole("button", { name }));
    expect(callback).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: "Session overview" })).toBeInTheDocument();
    act(() => accept?.());
    expect(callback).toHaveBeenCalledOnce();
  }
  fireEvent.click(screen.getByRole("button", { name: "Hide session overview" }));
  expect(dismiss).toHaveBeenCalledOnce();
});

it("fetches nested agents only when expanded and stops at the shared depth boundary", () => {
  vi.mocked(useChildSessions).mockClear();
  render(
    <MemoryRouter>
      <SessionOverview
        sessionId="parent"
        canBrowseFiles
        guard={(_path, _options, commit) => commit()}
        onDismiss={vi.fn()}
      />
    </MemoryRouter>,
  );
  expect(useChildSessions).not.toHaveBeenCalledWith("child 0");
  fireEvent.click(screen.getByRole("button", { name: "Expand Agent 0" }));
  expect(useChildSessions).toHaveBeenCalledWith("child 0");
  fireEvent.click(screen.getByRole("button", { name: "Expand Nested" }));
  expect(screen.getByRole("link", { name: "Leaf$0.10" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Expand Leaf" })).toBeNull();
  expect(useChildSessions).not.toHaveBeenCalledWith("leaf");
});
