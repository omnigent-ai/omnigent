import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BridgeSetupApp } from "./server-selector-v2";

afterEach(() => {
  cleanup();
  delete (window as { omnigentSetup?: unknown }).omnigentSetup;
});

/** Install a minimal `omnigentSetup` bridge; `over` replaces its defaults. */
function stubBridge(over: Record<string, unknown>) {
  (window as { omnigentSetup?: unknown }).omnigentSetup = {
    getServerUrl: async () => null,
    getRecentServers: async () => ["https://team.example.com/"],
    getManagedServers: async () => [],
    getCliStatus: async () => ({ installed: true }),
    copyText: async () => {},
    startLocalServer: async () => ({ ok: false }),
    ...over,
  };
}

it("tags a connect with a request ID, shows only its phases, and cancels it", async () => {
  let progress: (p: { requestId?: string; phase?: string }) => void = () => {};
  let finish: (r: { cancelled?: boolean }) => void = () => {};
  const setServerUrl = vi.fn(
    (_url: string, _opts?: { requestId?: string }) =>
      new Promise<{ cancelled?: boolean }>((resolve) => {
        finish = resolve;
      }),
  );
  const cancelServerConnection = vi.fn().mockResolvedValue(true);
  stubBridge({
    setServerUrl,
    cancelServerConnection,
    onConnectionProgress: (cb: typeof progress) => {
      progress = cb;
      return () => {};
    },
  });
  render(<BridgeSetupApp />);
  const openButton = await screen.findByRole("button", { name: /open omnigent/i });
  await waitFor(() => expect(openButton).toBeEnabled());
  fireEvent.click(openButton);
  await waitFor(() => expect(setServerUrl).toHaveBeenCalledTimes(1));
  const requestId = setServerUrl.mock.calls[0][1]?.requestId;
  expect(requestId).toEqual(expect.any(String));

  act(() => progress({ requestId: "someone-else", phase: "authenticating" }));
  expect(screen.queryByText(/finish signing in/i)).not.toBeInTheDocument();
  act(() => progress({ requestId, phase: "authenticating" }));
  expect(screen.getByText(/finish signing in/i)).toBeInTheDocument();

  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Cancel" })));
  expect(cancelServerConnection).toHaveBeenCalledWith(requestId);
  // The shell's late result after a confirmed cancel reads as cancelled: idle, no error.
  await act(async () => finish({}));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("a cancel from the shell (workspace picker closed) fails the terminal with Retry", async () => {
  stubBridge({
    getManagedServers: async () => ["https://team.example.com/"],
    getRecentServers: async () => [],
    getRunnerOptions: async () => ({ remote: false, bundledCli: true }),
    connectRunner: async () => ({ ok: true }),
    setServerUrl: async () => ({ cancelled: true }),
  });
  render(<BridgeSetupApp />);
  fireEvent.click(await screen.findByRole("button", { name: /join your team/i }));
  fireEvent.click(await screen.findByRole("button", { name: /open omnigent/i }));
  expect(await screen.findByText("Connection cancelled.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  expect(screen.queryByText(/server ready/i)).not.toBeInTheDocument();
});
