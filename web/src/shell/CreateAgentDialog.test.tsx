import type * as AgentLabelsModule from "@/lib/agentLabels";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentBundleInput } from "@/lib/agentBundle";
import { CreateAgentDialog } from "./CreateAgentDialog";

vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
}));

afterEach(cleanup);

describe("CreateAgentDialog MCP integrations", () => {
  it("adds an editable Azure DevOps stdio server without requesting a secret", async () => {
    const onCreate = vi.fn<(input: AgentBundleInput) => void>();
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={onCreate} />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    expect(await screen.findByText("Custom server")).toBeTruthy();
    expect(screen.getByText("Microsoft Learn")).toBeTruthy();
    expect(screen.getByText("AWS Knowledge")).toBeTruthy();
    expect(screen.getByText("Azure")).toBeTruthy();
    expect(screen.getByText("Context7")).toBeTruthy();
    expect(screen.getByText("DeepWiki")).toBeTruthy();
    expect(screen.getByText("Exa Search")).toBeTruthy();
    expect(screen.queryByText("No auth")).toBeNull();
    expect(screen.queryByText("Uses local login")).toBeNull();
    expect(screen.getByRole("listbox")).toHaveClass("overflow-y-auto", "overscroll-contain");
    fireEvent.click(screen.getByTestId("create-agent-add-preset-azure-devops"));
    expect(await screen.findByText("Azure DevOps")).toBeTruthy();
    expect(screen.getByText(/Node.js 20\+/)).toBeTruthy();
    expect(screen.queryByText(/personal access token/i)).toBeNull();

    const addButton = screen.getByTestId("mcp-preset-add");
    expect(addButton).toBeDisabled();
    fireEvent.change(screen.getByTestId("mcp-preset-field-organization"), {
      target: { value: "contoso" },
    });
    fireEvent.click(addButton);

    await waitFor(() =>
      expect(screen.getByTestId("create-agent-mcp-name")).toHaveValue("azure-devops"),
    );
    expect(screen.getByTestId("create-agent-mcp-command")).toHaveValue("npx");
    expect(screen.getByTestId("create-agent-mcp-args")).toHaveValue(
      "-y @azure-devops/mcp contoso --authentication azcli",
    );
    expect(screen.getByTestId("create-agent-mcp-env")).toHaveValue("");

    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "ado-agent" },
    });
    fireEvent.change(screen.getByTestId("create-agent-model"), {
      target: { value: "claude-sonnet-4-20250514" },
    });
    fireEvent.click(screen.getByTestId("create-agent-submit"));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        mcpServers: [
          {
            name: "azure-devops",
            transport: "stdio",
            command: "npx",
            args: ["-y", "@azure-devops/mcp", "contoso", "--authentication", "azcli"],
          },
        ],
      }),
    );
  });

  it("searches integrations across provider and category metadata", async () => {
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.change(await screen.findByTestId("mcp-integration-search"), {
      target: { value: "cloudflare" },
    });

    expect(screen.getByText("Cloudflare Docs")).toBeTruthy();
    expect(screen.getByText("Cloudflare Agents SDK Docs")).toBeTruthy();
    expect(screen.queryByText("Microsoft Learn")).toBeNull();
  });

  it("adds Microsoft Learn without additional configuration", async () => {
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.click(await screen.findByTestId("create-agent-add-preset-microsoft-learn"));

    expect(screen.getByTestId("create-agent-mcp-name")).toHaveValue("microsoft-learn");
    expect(screen.getByTestId("create-agent-mcp-transport")).toHaveTextContent("http");
    expect(screen.getByTestId("create-agent-mcp-url")).toHaveValue(
      "https://learn.microsoft.com/api/mcp",
    );
  });

  it("renders and adds a non-Microsoft integration through the same registry", async () => {
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    expect(await screen.findByText("Documentation")).toBeTruthy();
    expect(screen.getAllByText("AWS").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByTestId("create-agent-add-preset-aws-knowledge"));

    expect(screen.getByTestId("create-agent-mcp-name")).toHaveValue("aws-knowledge");
    expect(screen.getByTestId("create-agent-mcp-url")).toHaveValue(
      "https://knowledge-mcp.global.api.aws",
    );
  });

  it("adds Azure in read-only mode using local credentials", async () => {
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.click(await screen.findByTestId("create-agent-add-preset-azure"));

    expect(screen.getByTestId("create-agent-mcp-name")).toHaveValue("azure");
    expect(screen.getByTestId("create-agent-mcp-command")).toHaveValue("npx");
    expect(screen.getByTestId("create-agent-mcp-args")).toHaveValue(
      "-y @azure/mcp@latest server start --mode single --read-only",
    );
  });

  it("keeps manual MCP configuration as the primary neutral path", async () => {
    render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Add Azure DevOps" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.click(await screen.findByTestId("create-agent-add-custom-mcp"));

    expect(screen.getByTestId("create-agent-mcp-name")).toHaveValue("");
    expect(screen.getByTestId("create-agent-mcp-command")).toHaveValue("");
  });
});
