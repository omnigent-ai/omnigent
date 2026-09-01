import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServerSelectStep } from "./ServerSelectStep";

afterEach(cleanup);

const baseProps = {
  initialUrl: "http://localhost:6767",
  recentServers: [] as string[],
  managedServers: [] as string[],
  onBack: vi.fn(),
  onCopy: vi.fn(),
};

describe("ServerSelectStep", () => {
  it("Join connects to the selected recent server", async () => {
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

  it("shows the confirm warning when a URL doesn't look like Omnigent, then forces on the second click", async () => {
    const onConnect = vi
      .fn()
      .mockResolvedValueOnce({ needsConfirm: true })
      .mockResolvedValueOnce({});
    render(
      <ServerSelectStep {...baseProps} recentServers={["https://amazon.com/"]} onConnect={onConnect} />,
    );

    const join = screen.getByRole("button", { name: "Join" });
    fireEvent.click(join);
    expect(await screen.findByRole("alert")).toHaveTextContent(/doesn't look like an Omnigent server/i);

    // Second click on the same URL forces through.
    fireEvent.click(join);
    expect(onConnect).toHaveBeenNthCalledWith(2, "https://amazon.com/", true);
  });

  it("surfaces a rejected-connect error instead of silently doing nothing", async () => {
    const onConnect = vi.fn().mockResolvedValue({ error: "That server rejected the connection." });
    render(
      <ServerSelectStep {...baseProps} recentServers={["https://x.example.com/"]} onConnect={onConnect} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That server rejected the connection.");
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
    // Open the recent server's menu (radix opens on pointerDown) → it has Delete.
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /More options for mine.example.com/ }),
      { button: 0 },
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete from list" }));
    expect(onRemove).toHaveBeenCalledWith("https://mine.example.com/");
  });
});
