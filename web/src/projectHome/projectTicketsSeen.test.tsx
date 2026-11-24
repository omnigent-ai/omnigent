import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { getTicketCreationSummary } from "@/lib/projectTicketsApi";
import {
  markProjectTicketsSeen,
  readProjectTicketsSeen,
  useMarkProjectTicketsSeen,
  useNewTicketCount,
  useNewTicketsOnOpen,
} from "./projectTicketsSeen";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ project_ticket_summary: true }),
}));
vi.mock("@/lib/projectTicketsApi", () => ({ getTicketCreationSummary: vi.fn() }));

const summary = vi.mocked(getTicketCreationSummary);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  window.localStorage.clear();
  summary.mockReset();
});

it("treats existing tickets as seen the first time a project is listed", async () => {
  summary.mockResolvedValue({ latest_created_at: 500 });
  const { result } = renderHook(() => useNewTicketCount("p1"), { wrapper });
  await waitFor(() => expect(readProjectTicketsSeen("p1")).toBe(500));
  expect(result.current).toBe(0);
  expect(summary).toHaveBeenCalledWith("p1", undefined);
});

it("counts tickets created after the seen marker until the board is opened", async () => {
  markProjectTicketsSeen("p1", 500);
  summary.mockImplementation(async (_projectId, createdAfter) => ({
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
  summary.mockResolvedValue({});
  const { result } = renderHook(() => useNewTicketCount("p2"), { wrapper });
  await waitFor(() => expect(summary).toHaveBeenCalled());
  expect(result.current).toBe(0);
  expect(readProjectTicketsSeen("p2")).toBeUndefined();
});

it("marks tickets created since the board was last opened until each is opened", () => {
  markProjectTicketsSeen("p1", 500);
  const older = { id: "old", created_at: 400 };
  const created = { id: "new", created_at: 600 };
  const view = renderHook(({ open }) => useNewTicketsOnOpen("p1", open), {
    initialProps: { open: null as string | null },
  });
  // Opening the board advances the marker; the visit still knows what was new.
  markProjectTicketsSeen("p1", 600);
  view.rerender({ open: null });
  expect(view.result.current(older)).toBe(false);
  expect(view.result.current(created)).toBe(true);
  view.rerender({ open: "new" });
  expect(view.result.current(created)).toBe(false);
});

it("marks nothing on a board the viewer has never opened", () => {
  const { result } = renderHook(() => useNewTicketsOnOpen("p9", null));
  expect(result.current({ id: "t", created_at: 1 })).toBe(false);
});
