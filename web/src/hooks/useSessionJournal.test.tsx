import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { sessionJournalKey, type JournalPage } from "@/lib/sessionJournal";
import { noteSessionStreamClosed, noteSessionStreamOpen } from "@/lib/sessionStreamSignals";
import { journalEntry, journalPage, journalPlan } from "@/test/journalFixtures";
import {
  applyJournalEntry,
  JOURNAL_FALLBACK_REFETCH_MS,
  useSessionJournal,
} from "./useSessionJournal";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const SESSION = "conv_journal";
const RUN = "0b6c5f0d8a7e4c3b9a1d2e3f4a5b6c7d";

let queryClient: QueryClient;
// Pages for plain reads, in order, and the page a `kind=` read gets.
let queued: JournalPage[];
let byKind: Record<string, JournalPage>;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function respond(...pages: JournalPage[]) {
  queued.push(...pages);
}

function requestedQueries(): URLSearchParams[] {
  return vi
    .mocked(authenticatedFetch)
    .mock.calls.map(([url]) => new URL(String(url), "http://test").searchParams);
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queued = [];
  byKind = {};
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
    const kind = new URL(String(url), "http://test").searchParams.get("kind");
    const page = kind ? byKind[kind] : queued.shift();
    return new Response(JSON.stringify(page ?? journalPage([])));
  });
});

afterEach(() => {
  vi.useRealTimers();
  noteSessionStreamClosed(SESSION);
});

describe("useSessionJournal", () => {
  it("loads the newest page, then older pages only on request", async () => {
    respond(journalPage([journalEntry(2), journalEntry(1)]));
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() => expect(result.current.entries).toHaveLength(2));
    expect(result.current.entries.map((entry) => entry.position)).toEqual([1, 2]);
    expect(result.current.hasOlder).toBe(false);
    expect(requestedQueries().map((query) => Object.fromEntries(query))).toEqual([
      { order: "desc", limit: "100" },
    ]);
  });

  it("reads the newest plan and statuses when older pages exist", async () => {
    const plan = journalPlan(0, ["a"]);
    const started = journalEntry(1, { kind: "status", step: "a", payload: { status: "started" } });
    byKind = { plan: journalPage([plan]), status: journalPage([started]) };
    respond(journalPage([journalEntry(3), journalEntry(2)], { has_more: true }));
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() =>
      expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1, 2, 3]),
    );
    expect(result.current.hasOlder).toBe(true);
    expect(requestedQueries().map((query) => Object.fromEntries(query))).toEqual([
      { order: "desc", limit: "100" },
      { kind: "plan", order: "desc", limit: "1" },
      { kind: "status", order: "desc", limit: "1000" },
    ]);

    respond(journalPage([started, plan]));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.hasOlder).toBe(false));
    expect(requestedQueries()[3].get("after")).toBe("e2");
    expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1, 2, 3]);
  });

  it("adds live entries once, including entries written during an automation run", async () => {
    respond(journalPage([journalEntry(0)]));
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() => expect(result.current.entries).toHaveLength(1));

    act(() => {
      applyJournalEntry(queryClient, SESSION, journalEntry(1));
      applyJournalEntry(queryClient, SESSION, journalEntry(1));
      applyJournalEntry(queryClient, SESSION, journalEntry(2, { scheduled_task_run_id: RUN }));
    });
    await waitFor(() =>
      expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1, 2]),
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("catches up when the stream reopens, and polls only while it is down", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    noteSessionStreamOpen(SESSION, false);
    respond(journalPage([journalEntry(0)]));
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() => expect(result.current.entries).toHaveLength(1));

    // Open stream: no polling.
    await act(async () => vi.advanceTimersByTimeAsync(JOURNAL_FALLBACK_REFETCH_MS * 2));
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);

    // Down: catch up every 30 s from the newest entry.
    act(() => noteSessionStreamClosed(SESSION));
    respond(journalPage([journalEntry(1)]));
    await act(async () => vi.advanceTimersByTimeAsync(JOURNAL_FALLBACK_REFETCH_MS));
    await waitFor(() => expect(result.current.entries).toHaveLength(2));
    expect(requestedQueries()[1].get("after")).toBe("e0");

    // Reopened: one catch-up, and polling stops.
    respond(journalPage([journalEntry(2)]));
    act(() => noteSessionStreamOpen(SESSION, true));
    await waitFor(() => expect(result.current.entries).toHaveLength(3));
    expect(requestedQueries()[2].get("after")).toBe("e1");
    await act(async () => vi.advanceTimersByTimeAsync(JOURNAL_FALLBACK_REFETCH_MS * 2));
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);
  });

  it("reads nothing while disabled or for a provisional session", () => {
    renderHook(() => useSessionJournal(SESSION, false), { wrapper });
    renderHook(() => useSessionJournal("temp:pending", true), { wrapper });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(queryClient.getQueryData(sessionJournalKey(SESSION))).toBeUndefined();
  });
});
