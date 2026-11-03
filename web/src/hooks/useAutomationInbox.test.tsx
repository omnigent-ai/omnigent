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
  task_id: "task",
  task_name: "Report",
  run_id: "run",
  scheduled_at: 10,
  finished_at: 11,
  error_code: "host_offline",
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
  vi.mocked(listAutomationFailures).mockReset().mockResolvedValue({ data: [], next_cursor: null });
});
afterEach(() => {
  cleanup();
  client.clear();
});

it("continues across empty task pages and hides only the read occurrence", async () => {
  vi.mocked(listAutomationFailures)
    .mockResolvedValueOnce({ data: [], next_cursor: "next" })
    .mockResolvedValueOnce({ data: [failure], next_cursor: null });
  const { result } = renderHook(() => useAutomationInbox(true), { wrapper });
  await waitFor(() => expect(result.current.hasNextPage).toBe(true));
  await act(() => result.current.fetchNextPage());
  expect(listAutomationFailures).toHaveBeenLastCalledWith("next");
  await waitFor(() => expect(result.current.items).toEqual([failure]));
  act(() => result.current.markRead("run"));
  expect(result.current.items).toEqual([]);
  vi.mocked(listAutomationFailures).mockResolvedValue({
    data: [{ ...failure, run_id: "new-run" }],
    next_cursor: null,
  });
  await act(() => result.current.refetch());
  await waitFor(() => expect(result.current.items[0]?.run_id).toBe("new-run"));
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [], next_cursor: null });
  await act(() => result.current.refetch());
  await waitFor(() => expect(result.current.items).toEqual([]));
});

it("never fetches on an older server or before identity is ready", async () => {
  supported = false;
  const { rerender } = renderHook(() => useAutomationInbox(false), { wrapper });
  await act(async () => {});
  expect(listAutomationFailures).not.toHaveBeenCalled();
  supported = true;
  rerender();
  await act(async () => {});
  expect(listAutomationFailures).not.toHaveBeenCalled();
});

it("does not display the previous viewer's cached failures", async () => {
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [failure], next_cursor: null });
  const { result, rerender } = renderHook(() => useAutomationInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  vi.mocked(listAutomationFailures).mockResolvedValue({ data: [], next_cursor: null });
  viewer = "bob";
  rerender();
  expect(result.current.items).toEqual([]);
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.items).toEqual([]);
});
