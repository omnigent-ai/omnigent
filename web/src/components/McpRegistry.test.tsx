import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpRegistryConnections, McpRegistryPicker } from "./McpRegistry";
import { authorizeRegistryService, registryRequest } from "@/lib/mcpRegistry";

vi.mock("@/lib/mcpRegistry", () => ({
  registryRequest: vi.fn(),
  authorizeRegistryService: vi.fn(),
}));

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

describe("MCP service selection", () => {
  it("shows selected services and toggles membership without disconnecting the account", async () => {
    vi.mocked(registryRequest).mockResolvedValue({
      data: [
        service,
        { ...service, id: "connected", title: "Connected tracker", connected: true },
        { ...service, id: "attached", title: "Attached tracker", connected: true },
      ],
    });
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={["attached"]} onToggle={toggle} busy={false} />);
    const selected = await screen.findByRole("checkbox", { name: "Attached tracker" });
    expect(selected).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Work tracker" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Connected tracker" }));
    fireEvent.click(selected);
    expect(toggle.mock.calls).toEqual([
      ["connected", true],
      ["attached", false],
    ]);
    expect(registryRequest).toHaveBeenCalledTimes(1);
  });

  it("finishes sign-in before enabling a service", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest).mockResolvedValueOnce({ data: [oauth] });
    let complete!: () => void;
    vi.mocked(authorizeRegistryService).mockReturnValue(
      new Promise<void>((resolve) => {
        complete = resolve;
      }),
    );
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Work tracker" }));
    expect(toggle).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...oauth, connected: true }] });
    complete();
    await waitFor(() => expect(toggle).toHaveBeenCalledWith("tracker", true));
  });

  it("keeps a service unchecked after OAuth cancellation and allows retry", async () => {
    vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...service, auth: "oauth" }] });
    vi.mocked(authorizeRegistryService).mockRejectedValue(new Error("Sign-in cancelled"));
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Work tracker" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in cancelled");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("checkbox")).not.toBeDisabled();
    expect(toggle).not.toHaveBeenCalled();
  });

  it("reconnects an attached service without creating a duplicate attachment", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [oauth] })
      .mockResolvedValue({ data: [{ ...oauth, connected: true }] });
    vi.mocked(authorizeRegistryService).mockResolvedValue();
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={["tracker"]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reconnect" }));
    await screen.findByText("Connected");
    expect(toggle).not.toHaveBeenCalled();
  });

  it("allows removing disconnected or unavailable services and protects custom server names", async () => {
    vi.mocked(registryRequest).mockResolvedValue({
      data: [service, { ...service, id: "custom", title: "Custom conflict", connected: true }],
    });
    const toggle = vi.fn();
    render(
      <McpRegistryPicker
        attached={["tracker", "removed"]}
        reservedNames={["custom"]}
        onToggle={toggle}
        busy={false}
      />,
    );
    fireEvent.click(await screen.findByRole("checkbox", { name: "Work tracker" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "removed" }));
    expect(toggle.mock.calls).toEqual([
      ["tracker", false],
      ["removed", false],
    ]);
    expect(screen.getByRole("checkbox", { name: "Custom conflict" })).toBeDisabled();
  });
});
