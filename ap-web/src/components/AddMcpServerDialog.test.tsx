import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bundleManipulation", () => ({
  isValidMcpServerName: (name: string) => /^[A-Za-z0-9_-]+$/.test(name) && name.length <= 64,
}));

import { AddMcpServerDialog, type McpServerFormResult } from "./AddMcpServerDialog";

afterEach(cleanup);

function renderDialog(overrides: { onAdd?: (s: McpServerFormResult) => void } = {}) {
  const onAdd = overrides.onAdd ?? vi.fn();
  const onOpenChange = vi.fn();
  render(<AddMcpServerDialog open onOpenChange={onOpenChange} onAdd={onAdd} />);
  return { onAdd, onOpenChange };
}

describe("AddMcpServerDialog", () => {
  it("renders all form fields for stdio transport", () => {
    renderDialog();
    expect(screen.getByTestId("add-mcp-name")).toBeInTheDocument();
    expect(screen.getByTestId("add-mcp-transport")).toBeInTheDocument();
    expect(screen.getByTestId("add-mcp-command")).toBeInTheDocument();
    expect(screen.getByTestId("add-mcp-args")).toBeInTheDocument();
    expect(screen.getByTestId("add-mcp-env")).toBeInTheDocument();
  });

  it("disables submit when name is empty", () => {
    renderDialog();
    expect(screen.getByTestId("add-mcp-submit")).toBeDisabled();
  });

  it("disables submit when name is invalid", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("add-mcp-name"), { target: { value: "../evil" } });
    fireEvent.change(screen.getByTestId("add-mcp-command"), { target: { value: "echo" } });
    expect(screen.getByTestId("add-mcp-submit")).toBeDisabled();
  });

  it("shows validation hint for invalid names", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("add-mcp-name"), { target: { value: "bad/name" } });
    expect(screen.getByText(/Letters, digits, hyphens, underscores only/)).toBeInTheDocument();
  });

  it("enables submit with valid name and command", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("add-mcp-name"), { target: { value: "myserver" } });
    fireEvent.change(screen.getByTestId("add-mcp-command"), { target: { value: "npx" } });
    expect(screen.getByTestId("add-mcp-submit")).toBeEnabled();
  });

  it("calls onAdd with stdio server data on submit", () => {
    const onAdd = vi.fn();
    renderDialog({ onAdd });
    fireEvent.change(screen.getByTestId("add-mcp-name"), { target: { value: "github" } });
    fireEvent.change(screen.getByTestId("add-mcp-command"), { target: { value: "npx" } });
    fireEvent.change(screen.getByTestId("add-mcp-args"), { target: { value: "-y mcp-github" } });
    fireEvent.change(screen.getByTestId("add-mcp-env"), {
      target: { value: "TOKEN=abc123" },
    });
    fireEvent.click(screen.getByTestId("add-mcp-submit"));
    expect(onAdd).toHaveBeenCalledWith({
      name: "github",
      transport: "stdio",
      command: "npx",
      args: ["-y", "mcp-github"],
      env: { TOKEN: "abc123" },
    });
  });

  it("disables submit when command is empty for stdio", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("add-mcp-name"), { target: { value: "myserver" } });
    // command is empty
    expect(screen.getByTestId("add-mcp-submit")).toBeDisabled();
  });
});
