import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpRegistryConnections, McpRegistryPicker } from "./McpRegistry";
import { registryRequest } from "@/lib/mcpRegistry";

vi.mock("@/lib/mcpRegistry", () => ({ registryRequest: vi.fn() }));

const service = {
  id: "tracker",
  title: "Work tracker",
  description: "Read work tickets",
  auth: "bearer",
  connected: false,
  tools: ["read_ticket"],
  connect_provider: "bearer",
};

beforeEach(() => {
  vi.resetAllMocks();
});

describe("MCP account connections", () => {
  it("saves a personal token, clears it, and tests the connected service", async () => {
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [service] })
      .mockResolvedValueOnce({ connected: true })
      .mockResolvedValueOnce({ data: [{ ...service, connected: true }] })
      .mockResolvedValueOnce({ tools: ["read_ticket"] });
    render(<McpRegistryConnections />);
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
    const input = screen.getByLabelText("Token for Work tracker");
    expect(input.getAttribute("type")).toBe("password");
    fireEvent.change(input, { target: { value: "test-personal-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    await screen.findByText("Connected");
    expect(screen.queryByLabelText("Token for Work tracker")).toBeNull();
    expect(registryRequest).toHaveBeenCalledWith(
      "/tracker/connection",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ token: "test-personal-token" }),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connection works. Tools: read_ticket",
    );
  });

  it("shows a connection failure and keeps the account controls usable", async () => {
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [{ ...service, connected: true }] })
      .mockRejectedValueOnce(new Error("Reconnect your account"));
    render(<McpRegistryConnections />);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Reconnect your account");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Disconnect" })).not.toBeDisabled(),
    );
  });
});

it("only attaches connected services and hides already attached services", async () => {
  vi.mocked(registryRequest).mockResolvedValue({
    data: [
      service,
      { ...service, id: "connected", title: "Connected tracker", connected: true },
      { ...service, id: "attached", connected: true },
    ],
  });
  const add = vi.fn();
  render(<McpRegistryPicker attached={["attached"]} onAdd={add} busy={false} />);
  const button = await screen.findByRole("button", { name: "Add Connected tracker" });
  expect(screen.getByRole("button", { name: "Connect in settings" })).toBeDisabled();
  fireEvent.click(button);
  expect(add).toHaveBeenCalledWith("connected");
  expect(screen.getAllByRole("button")).toHaveLength(2);
});
