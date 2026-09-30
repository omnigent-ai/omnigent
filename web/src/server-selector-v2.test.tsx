import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BridgeSetupApp } from "./server-selector-v2";

afterEach(() => {
  cleanup();
  delete (window as { omnigentSetup?: unknown }).omnigentSetup;
});

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
  (window as { omnigentSetup?: unknown }).omnigentSetup = {
    getServerUrl: async () => null,
    getRecentServers: async () => ["https://team.example.com/"],
    getManagedServers: async () => [],
    getCliStatus: async () => ({ installed: true }),
    copyText: async () => {},
    startLocalServer: async () => ({ ok: false }),
    setServerUrl,
    cancelServerConnection,
    onConnectionProgress: (cb: typeof progress) => {
      progress = cb;
      return () => {};
    },
  };
  render(<BridgeSetupApp />);
  fireEvent.click(await screen.findByRole("button", { name: /open omnigent/i }));
  const requestId = setServerUrl.mock.calls[0][1]?.requestId;
  expect(requestId).toEqual(expect.any(String));

  act(() => progress({ requestId: "someone-else", phase: "authenticating" }));
  expect(screen.queryByText(/finish signing in/i)).not.toBeInTheDocument();
  act(() => progress({ requestId, phase: "authenticating" }));
  expect(screen.getByText(/finish signing in/i)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(cancelServerConnection).toHaveBeenCalledWith(requestId);
  // The shell's late result after a cancel reads as cancelled: idle, no error.
  await act(async () => finish({}));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
