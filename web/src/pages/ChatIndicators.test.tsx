import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { handleSessionEvent, useChatStore } from "@/store/chatStore";
import { McpStartupIndicator } from "./ChatIndicators";

const conversationId = "indicator-test-session";

beforeEach(() => {
  useChatStore.setState({
    conversationId,
    mcpStartup: null,
    mcpStartupLaunch: { pending: false, dismissed: false },
  });
});

afterEach(() => {
  cleanup();
  useChatStore.setState({
    conversationId: null,
    mcpStartup: null,
    mcpStartupLaunch: { pending: false, dismissed: false },
  });
});

describe("McpStartupIndicator lifecycle", () => {
  it("renders live progress and disappears when the round settles", () => {
    render(<McpStartupIndicator />);
    expect(screen.queryByTestId("mcp-startup-indicator")).toBeNull();

    act(() => {
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId,
        servers: {
          glean: { status: "starting", error: null },
          jira: { status: "starting", error: null },
          safe: { status: "starting", error: null },
        },
      });
    });
    expect(screen.getByTestId("mcp-startup-indicator")).toHaveTextContent(
      "Starting MCP servers (0/3): glean, jira, safe",
    );

    act(() => {
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId,
        servers: {
          glean: { status: "ready", error: null },
          jira: { status: "starting", error: null },
          safe: { status: "starting", error: null },
        },
      });
    });
    expect(screen.getByTestId("mcp-startup-indicator")).toHaveTextContent(
      "Starting MCP servers (1/3): jira, safe",
    );

    act(() => {
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId,
        servers: {
          glean: { status: "ready", error: null },
          jira: { status: "ready", error: null },
          safe: { status: "failed", error: "handshake failed" },
        },
      });
    });
    expect(screen.queryByTestId("mcp-startup-indicator")).toBeNull();
    expect(screen.queryByText(/MCP startup incomplete/i)).toBeNull();
  });

  it("hides a Stop-cancelled single-server round without adding a notice", () => {
    render(<McpStartupIndicator />);
    act(() => {
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId,
        servers: { storage: { status: "starting", error: null } },
      });
    });
    expect(screen.getByTestId("mcp-startup-indicator")).toHaveTextContent(
      "Starting MCP server: storage",
    );

    act(() => {
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId,
        servers: { storage: { status: "cancelled", error: null } },
      });
    });
    expect(screen.queryByTestId("mcp-startup-indicator")).toBeNull();
    expect(screen.queryByText(/MCP startup incomplete/i)).toBeNull();
  });
});
