import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { sessionJournalKey, sessionTaskList, type JournalPage } from "@/lib/sessionJournal";
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
    const params = new URL(String(url), "http://test").searchParams;
    const kind = params.get("kind");
    const step = params.get("step");
    const page = kind ? byKind[step ? `${kind}:${step}` : kind] : queued.shift();
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
    const plan = journalPlan(0, ["a"], ["b"]);
    const done = journalEntry(1, { kind: "status", step: "a", content: { status: "done" } });
    const busy = Array.from({ length: 1001 }, (_, index) =>
      journalEntry(index + 2, { kind: "status", step: "b", content: { status: "started" } }),
    ).reverse();
    byKind = {
      plan: journalPage([plan]),
      status: journalPage(busy.slice(0, 1000), { has_more: true }),
      "status:a": journalPage([done]),
      "status:b": journalPage([busy[0]]),
    };
    respond(journalPage([busy[0]], { has_more: true }));
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() =>
      expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1, 1002]),
    );
    expect(result.current.hasOlder).toBe(true);
    expect(requestedQueries().map((query) => Object.fromEntries(query))).toEqual([
      { order: "desc", limit: "100" },
      { kind: "plan", order: "desc", limit: "1" },
      { kind: "status", step: "a", order: "desc", limit: "1" },
      { kind: "status", step: "b", order: "desc", limit: "1" },
      { kind: "plan_status", order: "desc", limit: "1" },
    ]);

    respond(journalPage(busy.slice(1, 101), { has_more: true }));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.entries).toHaveLength(103));
    expect(requestedQueries()[5].get("after")).toBe("e1002");
    expect(result.current.entries).toContainEqual(done);
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

  it.each(["live", "paginated"])("resets prior run progress in %s journals", async (mode) => {
    const original = journalPlan(0, ["a"]);
    const done = journalEntry(1, { kind: "status", step: "a", content: { status: "done" } });
    const boundary = journalEntry(2, { kind: "plan_status", content: { status: "started" } });
    const next = journalPlan(3, ["a"]);
    if (mode === "live") respond(journalPage([done, original]));
    else {
      respond(journalPage([journalEntry(104)], { has_more: true }));
      byKind = {
        plan: journalPage([next]),
        "status:a": journalPage([done]),
        plan_status: journalPage([boundary]),
      };
    }
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    if (mode === "live") {
      expect(sessionTaskList(result.current.entries, [], true).progress[0].state).toBe("done");
      byKind.plan_status = journalPage([boundary]);
      act(() => applyJournalEntry(queryClient, SESSION, next));
    }
    await waitFor(() =>
      expect(sessionTaskList(result.current.entries, [], true).progress[0].state).toBe("ready"),
    );
    expect(result.current.entries).toContainEqual(done);
    expect(result.current.entries).toContainEqual(boundary);
  });

  it.each(["initial", "refetch", "reset"])(
    "keeps live entries received during an older %s snapshot",
    async (phase) => {
      noteSessionStreamOpen(SESSION, false);
      let finish!: (response: Response) => void;
      const defer = () =>
        vi.mocked(authenticatedFetch).mockImplementationOnce(
          () =>
            new Promise<Response>((resolve) => {
              finish = resolve;
            }),
        );
      if (phase === "initial") defer();
      else respond(journalPage([journalEntry(0)]));
      const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
      if (phase !== "initial") {
        await waitFor(() => expect(result.current.entries).toHaveLength(1));
        defer();
        act(() => {
          const filter = { queryKey: sessionJournalKey(SESSION), exact: true };
          if (phase === "reset") void queryClient.resetQueries(filter);
          else void queryClient.refetchQueries(filter);
        });
      }
      await waitFor(() => expect(finish).toBeTypeOf("function"));
      act(() => {
        applyJournalEntry(queryClient, SESSION, journalEntry(1));
        applyJournalEntry(queryClient, SESSION, journalEntry(1));
      });
      await act(async () => finish(new Response(JSON.stringify(journalPage([journalEntry(0)])))));
      await waitFor(() =>
        expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1]),
      );
    },
  );

  it.each(["plan", "status"])("reports a failed %s supplement and retries it", async (kind) => {
    const plan = journalPlan(0, ["a"]);
    const done = journalEntry(1, { kind: "status", step: "a", content: { status: "done" } });
    const newest = journalPage([journalEntry(2)], { has_more: true });
    byKind = { plan: journalPage([plan]), "status:a": journalPage([done]) };
    respond(newest);
    let failing = true;
    const fetch = vi.mocked(authenticatedFetch).getMockImplementation()!;
    vi.mocked(authenticatedFetch).mockImplementation((...args) => {
      const params = new URL(String(args[0]), "http://test").searchParams;
      return failing && params.get("kind") === kind
        ? Promise.resolve(new Response(null, { status: 500 }))
        : fetch(...args);
    });
    const { result } = renderHook(() => useSessionJournal(SESSION, true), { wrapper });
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.entries).toEqual([]);
    failing = false;
    respond(newest);
    act(() => result.current.retry());
    await waitFor(() =>
      expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1, 2]),
    );
    expect(result.current.error).toBeNull();
  });

  it.each(["none", "automatic", "manual"])(
    "catches up after reopening, with %s recovery",
    async (recovery) => {
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
      if (recovery !== "none") {
        vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(null, { status: 500 }));
      }
      act(() => noteSessionStreamOpen(SESSION, true));
      if (recovery !== "none") {
        await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
        expect(result.current.entries.map((entry) => entry.position)).toEqual([0, 1]);
        if (recovery === "manual") act(() => result.current.retry());
        else await act(async () => vi.advanceTimersByTimeAsync(JOURNAL_FALLBACK_REFETCH_MS));
      }
      await waitFor(() => expect(result.current.entries).toHaveLength(3));
      expect(result.current.error).toBeNull();
      expect(requestedQueries()[2].get("after")).toBe("e1");
      await act(async () => vi.advanceTimersByTimeAsync(JOURNAL_FALLBACK_REFETCH_MS * 2));
      expect(authenticatedFetch).toHaveBeenCalledTimes(recovery === "none" ? 3 : 4);
    },
  );

  it("reads nothing while disabled or for a provisional session", () => {
    renderHook(() => useSessionJournal(SESSION, false), { wrapper });
    renderHook(() => useSessionJournal("temp:pending", true), { wrapper });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(queryClient.getQueryData(sessionJournalKey(SESSION))).toBeUndefined();
  });
});
