import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ComposerContextResourceState, ComposerMcpSelection } from "@/lib/composerContext";
import { McpContextSelector } from "./McpContextSelector";
import { mcpContextOptionsFromServers, type McpContextOption } from "./mcpContextOptions";

const OPTIONS = mcpContextOptionsFromServers([
  {
    name: "github",
    transport: "http",
    description: "Repository tools",
    url: "https://mcp.example.com/private/path?token=secret",
  },
  {
    name: "jira",
    transport: "stdio",
    command: "/usr/local/bin/jira-mcp",
    args: ["--token", "secret"],
  },
]);

function ready(
  data: readonly McpContextOption[] = OPTIONS,
): ComposerContextResourceState<readonly McpContextOption[]> {
  return { status: "ready", data, error: null };
}

function renderSelector({
  resource = ready(),
  value = [],
  onChange = vi.fn(),
}: {
  resource?: ComposerContextResourceState<readonly McpContextOption[]>;
  value?: readonly ComposerMcpSelection[];
  onChange?: (value: ComposerMcpSelection[]) => void;
} = {}) {
  return {
    onChange,
    ...render(
      <McpContextSelector
        open
        onOpenChange={() => undefined}
        resource={resource}
        value={value}
        onChange={onChange}
      />,
    ),
  };
}

afterEach(cleanup);

describe("McpContextSelector", () => {
  it("represents intentionally empty context in the trigger, chip, and selected row", () => {
    renderSelector();
    expect(
      screen.getByRole("button", { name: /Choose MCP context.*No MCP context/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("No MCP")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /No MCP context/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("appends new selections without reordering existing context", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelector({ value: [{ id: "github", serverName: "github" }], onChange });
    await user.click(screen.getByRole("checkbox", { name: /jira/ }));
    expect(onChange).toHaveBeenCalledWith([
      { id: "github", serverName: "github" },
      { id: "jira", serverName: "jira" },
    ]);
  });

  it("renders ordered removable chips and unavailable saved selections", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelector({
      value: [
        { id: "jira", serverName: "jira" },
        { id: "retired", serverName: "retired-server" },
      ],
      onChange,
    });
    const chips = screen.getAllByRole("button", { name: /Remove .* from MCP context/ });
    expect(chips.map((chip) => chip.getAttribute("aria-label"))).toEqual([
      "Remove jira from MCP context",
      "Remove retired-server from MCP context",
    ]);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    await user.click(chips[0]);
    expect(onChange).toHaveBeenCalledWith([{ id: "retired", serverName: "retired-server" }]);
  });

  it("shows safe transport details without exposing paths, query strings, or args", () => {
    renderSelector();
    expect(screen.getByText("HTTP · mcp.example.com")).toBeInTheDocument();
    expect(screen.getByText("stdio · jira-mcp")).toBeInTheDocument();
    expect(screen.queryByText(/private\/path|token=secret|--token/)).toBeNull();
  });

  it("supports arrow-key navigation across the none and server rows", () => {
    renderSelector();
    const none = screen.getByRole("checkbox", { name: /No MCP context/ });
    const github = screen.getByRole("checkbox", { name: /github/ });
    none.focus();
    fireEvent.keyDown(none, { key: "ArrowDown" });
    expect(github).toHaveFocus();
    fireEvent.keyDown(github, { key: "ArrowUp" });
    expect(none).toHaveFocus();
  });

  it.each([
    ["loading", "Loading MCP servers…"],
    ["unavailable", "MCP context is unavailable for this agent."],
  ] as const)("renders the %s state", (status, text) => {
    renderSelector({ resource: { status, data: null, error: null } });
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("renders errors with a retry action", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <McpContextSelector
        open
        onOpenChange={() => undefined}
        resource={{ status: "error", data: null, error: new Error("Network unavailable") }}
        value={[]}
        onChange={() => undefined}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Network unavailable");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
