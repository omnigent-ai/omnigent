import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ServerRestartBanner } from "./ServerRestartBanner";
import { notifyServerRestart, SERVER_RESTART_MESSAGE } from "@/lib/serverRestart";

const authenticatedFetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  authenticatedFetch.mockReset();
});

describe("ServerRestartBanner", () => {
  it("says the server is restarting, then clears and refreshes once it's back", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(
      <QueryClientProvider client={client}>
        <ServerRestartBanner />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("server-restart-banner")).toBeNull();
    act(() => notifyServerRestart());
    expect(screen.getByRole("status")).toHaveTextContent(SERVER_RESTART_MESSAGE);

    // Still down: the banner stays.
    authenticatedFetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByTestId("server-restart-banner")).toBeInTheDocument();
    expect(invalidate).not.toHaveBeenCalled();

    // Back: the banner clears without a reload and the app's data refreshes.
    authenticatedFetch.mockResolvedValue(new Response("{}", { status: 200 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.queryByTestId("server-restart-banner")).toBeNull();
    expect(invalidate).toHaveBeenCalled();
  });
});
