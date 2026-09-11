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
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", false);
  });

  it("focusing the input deselects the recent and disables Join (exclusive)", () => {
    render(
      <ServerSelectStep
        {...baseProps}
        recentServers={["https://team.example.com/"]}
        onConnect={vi.fn()}
      />,
    );
    // A recent is pre-selected → Join enabled.
    expect(screen.getByRole("button", { name: "Join" })).toBeEnabled();
    // Focusing the input deselects → Join disabled; Add is the action now.
    fireEvent.focus(screen.getByLabelText("Server URL"));
    expect(screen.getByRole("button", { name: "Join" })).toBeDisabled();
  });

  it("Add validates, adds the URL to the list, selects it, and probes reachability", async () => {
    const onCheckServer = vi.fn().mockResolvedValue({ status: "ok" as const });
    render(<ServerSelectStep {...baseProps} onConnect={vi.fn()} onCheckServer={onCheckServer} />);

    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "my-server.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    // Added + selected → Join enabled, connects to the normalized URL.
    const normalized = "http://my-server.example.com/";
    expect(onCheckServer).toHaveBeenCalledWith(normalized);
    expect(screen.getByRole("button", { name: "Join" })).toBeEnabled();
    // The probe result surfaces on the card.
    await waitFor(() => expect(screen.getByText("Omnigent server")).toBeInTheDocument());
  });

  it("Add shows an error for an invalid URL and adds nothing", () => {
    const onCheckServer = vi.fn();
    render(<ServerSelectStep {...baseProps} onConnect={vi.fn()} onCheckServer={onCheckServer} />);
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "javascript:alert(1)" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/valid http\(s\) server URL/i);
    expect(onCheckServer).not.toHaveBeenCalled();
  });

  it("shows the confirm warning on Join, then forces on the second click", async () => {
    const onConnect = vi
      .fn()
      .mockResolvedValueOnce({ needsConfirm: true })
      .mockResolvedValueOnce({});
    render(
      <ServerSelectStep
        {...baseProps}
        recentServers={["https://amazon.com/"]}
        onConnect={onConnect}
      />,
    );
    const join = screen.getByRole("button", { name: "Join" });
    fireEvent.click(join);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /doesn't look like an Omnigent server/i,
    );
    fireEvent.click(join);
    expect(onConnect).toHaveBeenNthCalledWith(2, "https://amazon.com/", true);
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
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
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
