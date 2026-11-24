import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { getTicketSummary } from "@/lib/projectTicketsApi";
import {
  markProjectTicketsSeen,
  readProjectTicketsSeen,
  useMarkProjectTicketsSeen,
  useNewTicketCount,
} from "./projectTicketsSeen";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_ticket_summary: true }),
}));
vi.mock("@/lib/projectTicketsApi", () => ({ getTicketSummary: vi.fn() }));

const summary = vi.mocked(getTicketSummary);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  window.localStorage.clear();
  summary.mockReset();
});

it("treats existing tickets as seen the first time a project is listed", async () => {
  summary.mockResolvedValue({ needs_response_count: 0, is_complete: true, latest_created_at: 500 });
  const { result } = renderHook(() => useNewTicketCount("p1"), { wrapper });
  await waitFor(() => expect(readProjectTicketsSeen("p1")).toBe(500));
  expect(result.current).toBe(0);
  expect(summary).toHaveBeenCalledWith("p1", undefined);
});

it("counts tickets created after the seen marker until the board is opened", async () => {
  markProjectTicketsSeen("p1", 500);
  summary.mockImplementation(async (_projectId, createdAfter) => ({
    needs_response_count: 0,
    is_complete: true,
    latest_created_at: 700,
    ...(createdAfter === undefined ? {} : { created_after_count: createdAfter < 700 ? 2 : 0 }),
  }));
  const badge = renderHook(() => useNewTicketCount("p1"), { wrapper });
  await waitFor(() => expect(badge.result.current).toBe(2));
  expect(summary).toHaveBeenCalledWith("p1", 500);

  renderHook(() => useMarkProjectTicketsSeen("p1", true), { wrapper });
  await waitFor(() => expect(readProjectTicketsSeen("p1")).toBe(700));
  await waitFor(() => expect(badge.result.current).toBe(0));
});

it("never moves the marker backwards and ignores servers without the field", async () => {
  markProjectTicketsSeen("p1", 900);
  markProjectTicketsSeen("p1", 100);
  expect(readProjectTicketsSeen("p1")).toBe(900);
  summary.mockResolvedValue({ needs_response_count: 0, is_complete: true });
  const { result } = renderHook(() => useNewTicketCount("p2"), { wrapper });
  await waitFor(() => expect(summary).toHaveBeenCalled());
  expect(result.current).toBe(0);
  expect(readProjectTicketsSeen("p2")).toBeUndefined();
});
