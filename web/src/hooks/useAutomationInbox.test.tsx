import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { listAutomationFailures } from "@/lib/scheduledTasksApi";
import { useAutomationInbox } from "./useAutomationInbox";

vi.mock("@/lib/scheduledTasksApi", () => ({ listAutomationFailures: vi.fn() }));
let viewer: string | null = "alice";
vi.mock("./useViewerId", () => ({ useViewerId: () => viewer }));
let client: QueryClient;
let supported = true;
const failure = {
  taskId: "task",
  taskName: "Report",
  runId: "run",
  scheduledAt: 10,
  finishedAt: 11,
  errorCode: "host_offline",
};
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, scheduled_task_inbox: supported }}>
      {children}
    </CapabilitiesProvider>
  </QueryClientProvider>
);
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
  localStorage.clear();
  viewer = "alice";
  supported = true;
  vi.mocked(listAutomationFailures).mockReset().mockResolvedValue({ data: [], nextCursor: null });
});
afterEach(() => {
  cleanup();
  client.clear();
});

it("continues across empty task pages and hides only the read occurrence", async () => {
  vi.mocked(listAutomationFailures)
    .mockResolvedValueOnce({ data: [], nextCursor: "next" })
    .mockResolvedValueOnce({ data: [failure], nextCursor: null });
  const { result } = renderHook(() => useAutomationInbox(true), { wrapper });
  await waitFor(() => expect(result.current.hasNextPage).toBe(true));
  await act(() => result.current.fetchNextPage());
  expect(listAutomationFailures).toHaveBeenLastCalledWith("next");
  await waitFor(() => expect(result.current.items).toEqual([failure]));
  act(() => result.current.markRead("run"));
  expect(result.current.items).toEqual([]);
  vi.mocked(listAutomationFailures).mockResolvedValue({
    data: [{ ...failure, runId: "new-run" }],
    nextCursor: null,
  });
  await act(() => result.current.refetch());
  await waitFor(() => expect(result.current.items[0]?.runId).toBe("new-run"));
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [], nextCursor: null });
  await act(() => result.current.refetch());
  await waitFor(() => expect(result.current.items).toEqual([]));
});

it.each([
  { supported: false, identityReady: true },
  { supported: true, identityReady: false },
])("does not fetch with support=$supported and identityReady=$identityReady", async (inputs) => {
  supported = inputs.supported;
  renderHook(() => useAutomationInbox(inputs.identityReady), { wrapper });
  await act(async () => {});
  expect(listAutomationFailures).not.toHaveBeenCalled();
});

it("does not display the previous viewer's cached failures", async () => {
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [failure], nextCursor: null });
  const { result, rerender } = renderHook(() => useAutomationInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [], nextCursor: null });
  viewer = "bob";
  rerender();
  expect(result.current.items).toEqual([]);
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.items).toEqual([]);
});
