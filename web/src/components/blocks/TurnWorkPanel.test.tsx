import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BubbleView } from "@/components/chat/chatBubbleParts";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Bubble, RenderItem } from "@/lib/renderItems";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { useChatStore } from "@/store/chatStore";

function tool(
  callId: string,
  name: string,
  args: Record<string, unknown>,
  output: string,
): Extract<RenderItem, { kind: "tool" }> {
  return {
    kind: "tool",
    itemId: `fc_${callId}`,
    execution: {
      name,
      arguments: args,
      argsSummary: "",
      callId,
      agentName: "agent",
      executedBy: "server",
      output,
    },
    output,
    state: "output-available",
    startedAt: null,
    duration: 0.25,
  };
}

const SQL = "SELECT region, sum(amount) AS total FROM orders GROUP BY region";

function answerBubble(
  lifecycle: Extract<Bubble, { kind: "assistant" }>["lifecycle"] = "completed",
  items: RenderItem[] = [
    tool(
      "sql",
      "mcp__databricks__execute_sql",
      { statement: SQL, catalog: "main", schema: "sales" },
      JSON.stringify([
        { region: "west", total: 10 },
        { region: "east", total: 7 },
      ]),
    ),
    tool("read", "Read", { file_path: "/ws/notes/method.md" }, "# Method"),
    { kind: "text", itemId: "answer", text: "West leads with 10.", final: true },
  ],
): Extract<Bubble, { kind: "assistant" }> {
  return { kind: "assistant", responseId: "resp_1", stableId: "s1", lifecycle, error: null, items };
}

const openFile = vi.fn();
const FILE_VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: "/ws",
  workspaceHome: null,
};

function renderBubble(bubble: Bubble, isLastAssistant = false) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <TooltipProvider>
          <BubbleView bubble={bubble} isLastAssistant={isLastAssistant} />
        </TooltipProvider>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
}

const initialStoreState = useChatStore.getState();

afterEach(() => {
  cleanup();
  openFile.mockReset();
  useChatStore.setState(initialStoreState);
});

describe("Show your work", () => {
  it("lists the turn's queries and sources behind the Sources button", () => {
    renderBubble(answerBubble());
    const toggle = screen.getByRole("button", { name: "Sources" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("turn-work-panel")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByRole("region", { name: "Show your work" });
    expect(toggle).toHaveAttribute("aria-controls", panel.id);
    expect(panel).toHaveTextContent("1 query · 1 source");

    const query = within(panel).getByTestId("turn-work-query");
    expect(query).toHaveTextContent("Execute SQL");
    expect(query).toHaveTextContent("Databricks");
    expect(query).toHaveTextContent("Completed");
    expect(query).toHaveTextContent("2 rows");
    expect(query).toHaveTextContent("250ms");
    expect(query).toHaveTextContent(SQL);
    expect(query).toHaveTextContent("Source: main.sales");
    expect(within(query).getByRole("button", { name: "Copy query 1" })).toBeInTheDocument();

    // The full call opens in place with its input and result.
    fireEvent.click(within(query).getByRole("button", { name: "Full tool call for query 1" }));
    expect(within(query).getAllByTestId("tool-value-view").length).toBeGreaterThan(0);
    expect(query).toHaveTextContent("west");

    // A file read links to the file viewer, workspace-relative.
    const source = within(panel).getByTestId("turn-work-source");
    expect(source).toHaveTextContent("Read");
    fireEvent.click(within(source).getByRole("button", { name: "notes/method.md" }));
    expect(openFile).toHaveBeenCalledWith("notes/method.md");

    fireEvent.click(within(panel).getByRole("button", { name: "Close sources" }));
    expect(screen.queryByTestId("turn-work-panel")).toBeNull();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("shows a failed query's error", () => {
    renderBubble(
      answerBubble("completed", [
        tool("bad", "run_sql", { sql: "SELECT * FROM missing" }, "Error: table not found"),
        { kind: "text", itemId: "answer", text: "The table is missing.", final: true },
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Sources" }));
    const query = screen.getByTestId("turn-work-query");
    expect(query).toHaveTextContent("Failed");
    expect(within(query).getByTestId("tool-error-box")).toHaveTextContent("Error: table not found");
  });

  it("waits for a native harness's turn to end, whose bubble never streams", () => {
    useChatStore.setState({ sessionStatus: "running" });
    renderBubble(answerBubble("completed"), true);
    expect(screen.queryByRole("button", { name: "Sources" })).toBeNull();
    cleanup();

    // An earlier answer is settled even while a later turn runs.
    renderBubble(answerBubble("completed"), false);
    expect(screen.getByRole("button", { name: "Sources" })).toBeInTheDocument();
  });

  it("offers no Sources button while streaming or without data work", () => {
    renderBubble(answerBubble("streaming"));
    expect(screen.queryByRole("button", { name: "Sources" })).toBeNull();
    cleanup();

    renderBubble(
      answerBubble("completed", [
        tool("todo", "TodoWrite", { todos: [] }, "ok"),
        { kind: "text", itemId: "answer", text: "Planned.", final: true },
      ]),
    );
    expect(screen.queryByRole("button", { name: "Sources" })).toBeNull();
  });
});
