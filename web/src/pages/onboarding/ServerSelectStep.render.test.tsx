import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServerSelectStep } from "./ServerSelectStep";

afterEach(cleanup);

const baseProps = {
  initialUrl: "http://localhost:6767",
  recentServers: [] as string[],
  managedServers: [] as string[],
  onBack: vi.fn(),
  onCopy: vi.fn(),
  onCheckServer: vi.fn().mockResolvedValue({ status: "ok" as const }),
};

describe("ServerSelectStep", () => {
  it("Join connects to the pre-selected recent server", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    render(
      <ServerSelectStep
        {...baseProps}
        recentServers={["https://team.example.com/"]}
        onConnect={onConnect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /install omnigent/i }));
    expect(onConnect).toHaveBeenCalledWith("https://team.example.com/");
  });

  it("with recents, starts on the list (no input) and 'Add server' opens the add view", () => {
    render(
      <ServerSelectStep
        {...baseProps}
        recentServers={["https://team.example.com/"]}
        onConnect={vi.fn()}
      />,
    );
    // List mode: a recent is pre-selected → Install enabled, no URL input yet.
    expect(screen.getByRole("button", { name: /install omnigent/i })).toBeEnabled();
    expect(screen.queryByLabelText("Server URL")).not.toBeInTheDocument();
    // "Add server" switches to the add view: input appears, action becomes Join.
    fireEvent.click(screen.getByRole("button", { name: /add server/i }));
    expect(screen.getByLabelText("Server URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Join" })).toBeInTheDocument();
  });

  it("Add validates, adds the URL to the list, selects it, and probes reachability", async () => {
    const onCheckServer = vi.fn().mockResolvedValue({ status: "ok" as const });
    render(<ServerSelectStep {...baseProps} onConnect={vi.fn()} onCheckServer={onCheckServer} />);

    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "my-server.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Join" }));

    // Added + selected → Join enabled, connects to the normalized URL.
    const normalized = "http://my-server.example.com/";
    expect(onCheckServer).toHaveBeenCalledWith(normalized);
    expect(screen.getByRole("button", { name: /install omnigent/i })).toBeEnabled();
    // The probe result surfaces on the card.
    await waitFor(() => expect(screen.getByText("Omnigent server")).toBeInTheDocument());
  });

  it("Add shows an error for an invalid URL and adds nothing", () => {
    const onCheckServer = vi.fn();
    render(<ServerSelectStep {...baseProps} onConnect={vi.fn()} onCheckServer={onCheckServer} />);
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "javascript:alert(1)" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/valid http\(s\) server URL/i);
    expect(onCheckServer).not.toHaveBeenCalled();
  });

  it("surfaces a rejected-connect error instead of silently doing nothing", async () => {
    const onConnect = vi.fn().mockResolvedValue({ error: "That server rejected the connection." });
    render(
      <ServerSelectStep
        {...baseProps}
        recentServers={["https://x.example.com/"]}
        onConnect={onConnect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /install omnigent/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That server rejected the connection.",
    );
  });

  it("offers Delete from list only for recent (not managed) servers", () => {
    const onRemove = vi.fn();
    render(
      <ServerSelectStep
        {...baseProps}
        managedServers={["https://org.example.com/"]}
        recentServers={["https://mine.example.com/"]}
        onConnect={vi.fn().mockResolvedValue({})}
        onRemove={onRemove}
      />,
    );
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /More options for mine.example.com/ }),
      {
        button: 0,
      },
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete from list" }));
    expect(onRemove).toHaveBeenCalledWith("https://mine.example.com/");
  });
});
