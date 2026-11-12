import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { SandboxStatus } from "@/lib/types";
import { bindConversationForTest, handleSessionEvent, useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { McpStartupIndicator, SandboxActionBanner } from "./ChatIndicators";

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

describe("SandboxActionBanner", () => {
  const DETAILS = "Refresh your dbcert on your laptop.";

  /** Put *id* on screen, seeded with *sandboxStatus* when given. */
  function showSession(id: string, sandboxStatus?: SandboxStatus | null) {
    act(() => {
      bindConversationForTest(id, sandboxStatus === undefined ? undefined : { sandboxStatus });
    });
  }

  /** Deliver a launch report on the session's own stream, as the server does. */
  function report(id: string, stage: "action_required" | "provisioning") {
    act(() =>
      handleSessionEvent(
        {
          type: "session_sandbox_status",
          conversationId: id,
          stage,
          error: null,
          provider: "arclet",
          details: stage === "action_required" ? DETAILS : null,
        },
        id,
      ),
    );
  }

  afterEach(() => {
    cleanup();
    conversationRegistry.clear();
    bindConversationForTest(null);
  });

  it("says what the launch needs the user to do", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    expect(screen.getByRole("status")).toHaveTextContent(DETAILS);
  });

  it.each([
    ["no launch", null],
    ["a launch in progress", { stage: "provisioning", error: null }],
    ["a failed launch", { stage: "failed", error: "managed host did not come online" }],
    ["a report without details", { stage: "action_required", error: null }],
  ] as const)("stays hidden for %s", (_label, status) => {
    showSession("conv_a", status);
    render(<SandboxActionBanner />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("goes away once the launch moves on", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    report("conv_a", "provisioning");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps each session's dismissal", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
    showSession("conv_b");
    report("conv_b", "action_required");
    expect(screen.getByRole("status")).toHaveTextContent(DETAILS);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    showSession("conv_a");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps a dismissal when the session's status reloads with the same warning", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    // A rebind seeds the store from the session snapshot, a new object with the same warning.
    showSession("conv_a", { stage: "action_required", error: null, details: DETAILS });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps a dismissal when the banner remounts", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    // The transcript unmounts while the terminal is open, and mounts again after.
    cleanup();
    render(<SandboxActionBanner />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("clears a background session's dismissal once its launch moves on", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    showSession("conv_b");
    report("conv_a", "provisioning");
    report("conv_a", "action_required");
    showSession("conv_a");
    expect(screen.getByRole("status")).toHaveTextContent(DETAILS);
  });

  it("shows a later warning in the same session after a dismissal", () => {
    showSession("conv_a");
    report("conv_a", "action_required");
    render(<SandboxActionBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    report("conv_a", "provisioning");
    report("conv_a", "action_required");
    expect(screen.getByRole("status")).toHaveTextContent(DETAILS);
  });
});
