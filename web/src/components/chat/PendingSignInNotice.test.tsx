import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { getSessionSignInLink } from "@/lib/sessionsApi";
import { PENDING_SIGN_IN_POLL_MS, PendingSignInNotice } from "./PendingSignInNotice";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSignInLink: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.mocked(getSessionSignInLink).mockReset();
});

it("shows the sign-in card while the agent pane waits on a sign-in", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const signedOut = { pending: true, url: "https://signin.example.com/device", code: null };
  const idle = { pending: false, url: null, code: null };
  vi.mocked(getSessionSignInLink)
    .mockResolvedValueOnce(idle)
    .mockResolvedValueOnce(signedOut)
    .mockResolvedValue(idle);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PendingSignInNotice sessionId="conv_new" />
    </QueryClientProvider>,
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.queryByRole("button", { name: "Open sign-in link" })).toBeNull();

  // The launcher prints its prompt after the pane starts.
  await act(() => vi.advanceTimersByTimeAsync(PENDING_SIGN_IN_POLL_MS));
  expect(screen.getByRole("button", { name: "Open sign-in link" })).toBeInTheDocument();
  expect(screen.getByTestId("error-headline")).toHaveTextContent(
    "Sign in to Databricks to start the agent",
  );
  expect(getSessionSignInLink).toHaveBeenCalledWith("conv_new", expect.anything());

  await act(() => vi.advanceTimersByTimeAsync(PENDING_SIGN_IN_POLL_MS));
  expect(screen.queryByRole("button", { name: "Open sign-in link" })).toBeNull();
});
