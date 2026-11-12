import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { showToast } from "@/components/ui/toast";
import { setOmnigentHostConfig } from "@/lib/host";
import {
  listSuggestions,
  updateSuggestion,
  type Suggestion,
  type SuggestionPage,
  type SuggestionUpdateResult,
} from "@/lib/proactiveInbox";
import type * as ProactiveInboxModule from "@/lib/proactiveInbox";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { useProactiveInbox } from "./useProactiveInbox";

vi.mock("@/lib/proactiveInbox", async (importActual) => ({
  ...(await importActual<typeof ProactiveInboxModule>()),
  listSuggestions: vi.fn(),
  updateSuggestion: vi.fn(),
}));
vi.mock("./useViewerId", () => ({ useViewerId: () => "alice" }));
vi.mock("./useProactiveAgentSettings", () => ({
  useProactiveAgentSettings: () => ({ settings: enabled ? { enabled: true } : undefined }),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

let client: QueryClient;
let enabled = true;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <RoutingProvider value={basenamedRouting("/")}>{children}</RoutingProvider>
    </CapabilitiesProvider>
  </QueryClientProvider>
);

const item: Suggestion = {
  id: "sug",
  kind: "info",
  title: "t",
  bodyMd: "b",
  links: [],
  actions: [],
  media: [],
  sourceSessionId: "s",
  status: "unread",
  resolution: "none",
  createdAt: 1,
};

function page(data: Suggestion[], unreadCount: number): SuggestionPage {
  return { data, nextCursor: null, unreadCount, unreadCountCapped: false };
}

/** The server's reply to a PATCH: the item, and the viewer's unread total after it. */
function saved(suggestion: Suggestion, unreadCount = 0): SuggestionUpdateResult {
  return { item: suggestion, unreadCount, unreadCountCapped: false };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity, retry: false } } });
  enabled = true;
  setOmnigentHostConfig({ serverIdentity: "server-a" });
  vi.mocked(listSuggestions)
    .mockReset()
    .mockResolvedValue(page([item], 1));
  vi.mocked(updateSuggestion).mockReset();
  vi.mocked(showToast).mockReset();
});

afterEach(() => {
  cleanup();
  client.clear();
  setOmnigentHostConfig({});
});

it("stays off when the server does not offer suggestions to this caller", () => {
  enabled = false;
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });

  expect(result.current.items).toEqual([]);
  expect(listSuggestions).not.toHaveBeenCalled();
});

it("does not let a poll that lands mid-update restore the old state", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(1));

  // A poll that read the item before the dismissal reached the server.
  const stalePoll = deferred<SuggestionPage>();
  vi.mocked(listSuggestions).mockReturnValueOnce(stalePoll.promise);
  void act(() => void result.current.refetch());
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);

  await act(async () => stalePoll.resolve(page([item], 1)));
  expect(result.current.items).toEqual([]);

  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" })),
  );
  await waitFor(() => expect(listSuggestions).toHaveBeenLastCalledWith(null, ["unread", "read"]));
  expect(result.current.items).toEqual([]);
});

it("pages with the server cursor for the requested states", async () => {
  vi.mocked(listSuggestions)
    .mockResolvedValueOnce({ ...page([], 1), nextCursor: "10:abc" })
    .mockResolvedValueOnce(page([item], 1));
  const { result } = renderHook(() => useProactiveInbox(true, ["unread"]), { wrapper });

  await waitFor(() => expect(result.current.hasNextPage).toBe(true));
  await act(() => result.current.fetchNextPage());

  expect(listSuggestions).toHaveBeenLastCalledWith("10:abc", ["unread"]);
  await waitFor(() => expect(result.current.items).toEqual([item]));
});

it("keeps a suggestion resolved when an earlier read and a dismissal overlap", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  const read = deferred<SuggestionUpdateResult>();
  const dismiss = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion)
    .mockReturnValueOnce(read.promise)
    .mockReturnValueOnce(dismiss.promise);

  // Expanding marks the item read; the viewer dismisses it before that settles.
  act(() => void result.current.update("sug", { status: "read" }));
  act(() => void result.current.update("sug", { resolution: "not_relevant" }));
  expect(result.current.items).toEqual([]);
  // The dismissal waits for the read, so the server sees them in order.
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(updateSuggestion).toHaveBeenCalledTimes(1);

  // The read's response says the item is open; it must not bring it back.
  await act(async () => read.resolve(saved({ ...item, status: "read" })));
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(2));
  expect(vi.mocked(updateSuggestion).mock.calls[1]).toEqual([
    "sug",
    { resolution: "not_relevant" },
  ]);
  expect(result.current.items).toEqual([]);

  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () =>
    dismiss.resolve(saved({ ...item, status: "dismissed", resolution: "not_relevant" })),
  );
  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(2));
  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);
});

it("ignores a read mark on a suggestion the viewer already resolved", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  const dismiss = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(dismiss.promise);

  act(() => void result.current.update("sug", { resolution: "already_done" }));
  act(() => void result.current.update("sug", { status: "read" }));
  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () =>
    dismiss.resolve(saved({ ...item, status: "done", resolution: "already_done" })),
  );

  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(2));
  expect(updateSuggestion).toHaveBeenCalledTimes(1);
  expect(result.current.items).toEqual([]);
});

it("reports a failed update and reloads the list", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  vi.mocked(updateSuggestion).mockRejectedValueOnce(new Error("offline"));

  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);

  await waitFor(() => expect(showToast).toHaveBeenCalledOnce());
  // The server still has the item open, so the refetch shows it again.
  await waitFor(() => expect(result.current.items).toEqual([item]));
  expect(listSuggestions).toHaveBeenCalledTimes(2);
});

it("keeps the item when the update and the recovery fetch both fail", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  vi.mocked(updateSuggestion).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(listSuggestions).mockRejectedValueOnce(new Error("offline"));

  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);

  await waitFor(() => expect(showToast).toHaveBeenCalledOnce());
  // The server still has the item open; the card comes back so the viewer can retry.
  await waitFor(() => expect(result.current.items).toEqual([item]));
  await waitFor(() => expect(result.current.unreadCount).toBe(1));
});

it("lists only the requested states after a read mark", async () => {
  vi.mocked(updateSuggestion).mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useProactiveInbox(true, ["unread"]), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));

  act(() => void result.current.update("sug", { status: "read" }));

  expect(result.current.items).toEqual([]);
});

it.each(["fails", "hangs"])(
  "keeps a dismissal when a poll started during the PATCH carries the old item and the refetch %s",
  async (refetchOutcome) => {
    const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
    await waitFor(() => expect(result.current.unreadCount).toBe(1));
    const patch = deferred<SuggestionUpdateResult>();
    vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

    act(() => void result.current.update("sug", { resolution: "not_now" }));
    await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());
    // A poll that read the item before the server applied the dismissal lands
    // while the PATCH is still in flight.
    vi.mocked(listSuggestions).mockResolvedValueOnce(page([item], 1));
    await act(async () => void (await result.current.refetch()));
    expect(result.current.items).toEqual([]);

    if (refetchOutcome === "fails") {
      vi.mocked(listSuggestions).mockRejectedValue(new Error("offline"));
    } else {
      vi.mocked(listSuggestions).mockReturnValue(new Promise(() => {}));
    }
    await act(async () =>
      patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" })),
    );
    await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(3));

    expect(result.current.items).toEqual([]);
    expect(result.current.unreadCount).toBe(0);
  },
);

it("drops a poll still in flight when the PATCH settles", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(1));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);
  act(() => void result.current.update("sug", { resolution: "not_now" }));
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());
  const stalePoll = deferred<SuggestionPage>();
  vi.mocked(listSuggestions).mockReturnValueOnce(stalePoll.promise);
  void act(() => void result.current.refetch());

  vi.mocked(listSuggestions).mockReturnValue(new Promise(() => {}));
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" })),
  );
  await act(async () => stalePoll.resolve(page([item], 1)));

  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);
});

it("restores the last confirmed state when overlapping updates both fail", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  const read = deferred<SuggestionUpdateResult>();
  const dismiss = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion)
    .mockReturnValueOnce(read.promise)
    .mockReturnValueOnce(dismiss.promise);

  // Expanding marks the item read; the viewer dismisses it before that settles.
  act(() => void result.current.update("sug", { status: "read" }));
  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);

  // Both PATCHes fail, and so does the recovery fetch: the server never saw either.
  vi.mocked(listSuggestions).mockRejectedValue(new Error("offline"));
  await act(async () => read.reject(new Error("offline")));
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(2));
  expect(result.current.items).toEqual([]);
  await act(async () => dismiss.reject(new Error("offline")));
  await waitFor(() => expect(showToast).toHaveBeenCalledTimes(2));

  // Not the optimistic read mark: the item is unread, as the server has it.
  await waitFor(() => expect(result.current.items).toEqual([item]));
  expect(result.current.unreadCount).toBe(1);
});

it("restores the state the server confirmed, not the one before it, when a later update fails", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  const read = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion)
    .mockReturnValueOnce(read.promise)
    .mockRejectedValueOnce(new Error("offline"));

  act(() => void result.current.update("sug", { status: "read" }));
  act(() => void result.current.update("sug", { resolution: "not_now" }));
  vi.mocked(listSuggestions).mockRejectedValue(new Error("offline"));
  await act(async () => read.resolve(saved({ ...item, status: "read" })));
  await waitFor(() => expect(showToast).toHaveBeenCalledOnce());

  // The read reached the server; only the dismissal is undone.
  await waitFor(() => expect(result.current.items).toEqual([{ ...item, status: "read" }]));
  expect(result.current.unreadCount).toBe(0);
});

it("shows a pending update to every instance, so the sidebar badge agrees with the Inbox", async () => {
  const { result } = renderHook(
    () => ({ inbox: useProactiveInbox(true), sidebar: useProactiveInbox(true, ["unread"]) }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.sidebar.unreadCount).toBe(1));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

  act(() => void result.current.inbox.update("sug", { resolution: "not_now" }));
  expect(result.current.sidebar.unreadCount).toBe(0);
  expect(result.current.sidebar.items).toEqual([]);
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());

  // A poll that read the item before the dismissal lands on the sidebar's query.
  vi.mocked(listSuggestions).mockResolvedValueOnce(page([item], 1));
  await act(async () => void (await result.current.sidebar.refetch()));
  expect(result.current.sidebar.unreadCount).toBe(0);
  expect(result.current.sidebar.items).toEqual([]);
  expect(result.current.inbox.unreadCount).toBe(0);

  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" })),
  );
  await waitFor(() => expect(result.current.sidebar.unreadCount).toBe(0));
  expect(result.current.inbox.items).toEqual([]);
});

it("subtracts a dismissal from the unread total once, in every instance, across polls", async () => {
  // Three unread suggestions; one unread fixture would hide a second
  // decrement behind the zero clamp.
  const others = [
    { ...item, id: "two", createdAt: 2 },
    { ...item, id: "three", createdAt: 3 },
  ];
  vi.mocked(listSuggestions).mockResolvedValue(page([...others, item], 3));
  const { result } = renderHook(
    () => ({ inbox: useProactiveInbox(true), sidebar: useProactiveInbox(true, ["unread"]) }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.sidebar.unreadCount).toBe(3));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

  act(() => void result.current.inbox.update("sug", { resolution: "not_now" }));
  expect(result.current.inbox.unreadCount).toBe(2);
  expect(result.current.sidebar.unreadCount).toBe(2);
  expect(result.current.inbox.items).toEqual(others);
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());

  // A poll that read the list before the dismissal lands on both queries.
  await act(async () => void (await result.current.inbox.refetch()));
  await act(async () => void (await result.current.sidebar.refetch()));
  expect(result.current.inbox.unreadCount).toBe(2);
  expect(result.current.sidebar.unreadCount).toBe(2);

  vi.mocked(listSuggestions).mockResolvedValue(page(others, 2));
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" }, 2)),
  );
  await waitFor(() => expect(result.current.inbox.unreadCount).toBe(2));
  expect(result.current.sidebar.unreadCount).toBe(2);
  expect(result.current.inbox.items).toEqual(others);
});

it("does not subtract again when a poll already reflects the committed change, and keeps the server's total if the final refetch fails", async () => {
  const others = [
    { ...item, id: "two", createdAt: 2 },
    { ...item, id: "three", createdAt: 3 },
  ];
  vi.mocked(listSuggestions).mockResolvedValue(page([...others, item], 3));
  const { result } = renderHook(
    () => ({ inbox: useProactiveInbox(true), sidebar: useProactiveInbox(true, ["unread"]) }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.inbox.unreadCount).toBe(3));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

  act(() => void result.current.inbox.update("sug", { resolution: "not_now" }));
  expect(result.current.inbox.unreadCount).toBe(2);
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());

  // The server committed the dismissal; a poll sees two remaining before the
  // PATCH response arrives. Both lists must still show two, not one.
  vi.mocked(listSuggestions).mockResolvedValue(page(others, 2));
  await act(async () => void (await result.current.inbox.refetch()));
  await act(async () => void (await result.current.sidebar.refetch()));
  expect(result.current.inbox.unreadCount).toBe(2);
  expect(result.current.sidebar.unreadCount).toBe(2);
  expect(result.current.inbox.items).toEqual(others);

  // The response lands, and the refetch it triggers fails: the server's total stays.
  vi.mocked(listSuggestions).mockRejectedValue(new Error("offline"));
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" }, 2)),
  );
  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(6));
  expect(result.current.inbox.unreadCount).toBe(2);
  expect(result.current.sidebar.unreadCount).toBe(2);
  expect(result.current.inbox.items).toEqual(others);
});

it("settles a list first loaded before the server committed the dismissal on the server's total", async () => {
  // The Unread tab's page stops before the dismissed item, and the server has
  // not applied the dismissal yet: the page counts three. The total stays at
  // three only until the PATCH settles with the server's count.
  const others = [
    { ...item, id: "two", createdAt: 2 },
    { ...item, id: "three", createdAt: 3 },
  ];
  vi.mocked(listSuggestions).mockResolvedValue(page([...others, item], 3));
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(3));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);
  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.unreadCount).toBe(2);

  vi.mocked(listSuggestions).mockResolvedValue({ ...page(others, 3), nextCursor: "2:two" });
  const unread = renderHook(() => useProactiveInbox(true, ["unread"]), { wrapper });
  await waitFor(() => expect(unread.result.current.items).toEqual(others));
  expect(unread.result.current.unreadCount).toBe(3);

  // The server commits; its reply carries the total, and so does any refetch.
  vi.mocked(listSuggestions).mockResolvedValue({ ...page(others, 2), nextCursor: "2:two" });
  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" }, 2)),
  );
  expect(unread.result.current.unreadCount).toBe(2);
  expect(result.current.unreadCount).toBe(2);
});

it("sends updates to different suggestions one at a time, so an older total cannot overwrite a newer one", async () => {
  const a = { ...item, id: "a", createdAt: 3 };
  const b = { ...item, id: "b", createdAt: 2 };
  const c = { ...item, id: "c", createdAt: 1 };
  vi.mocked(listSuggestions).mockResolvedValue(page([a, b, c], 3));
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(3));
  const patchA = deferred<SuggestionUpdateResult>();
  const patchB = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion)
    .mockReturnValueOnce(patchA.promise)
    .mockReturnValueOnce(patchB.promise);

  act(() => void result.current.update("a", { resolution: "not_now" }));
  act(() => void result.current.update("b", { resolution: "not_now" }));
  expect(result.current.items).toEqual([c]);
  expect(result.current.unreadCount).toBe(1);
  // B's PATCH waits for A's reply, so the server's totals arrive in order.
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(updateSuggestion).toHaveBeenCalledTimes(1);

  await act(async () =>
    patchA.resolve(saved({ ...a, status: "dismissed", resolution: "not_now" }, 2)),
  );
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(2));
  // A's reply says two unread; B's pending dismissal still shows one.
  expect(result.current.unreadCount).toBe(1);

  vi.mocked(listSuggestions).mockRejectedValue(new Error("offline"));
  await act(async () =>
    patchB.resolve(saved({ ...b, status: "dismissed", resolution: "not_now" }, 1)),
  );
  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(2));
  expect(result.current.items).toEqual([c]);
  expect(result.current.unreadCount).toBe(1);
});

it("trusts the total of a list first loaded while the dismissal is in flight", async () => {
  const others = [
    { ...item, id: "two", createdAt: 2 },
    { ...item, id: "three", createdAt: 3 },
  ];
  vi.mocked(listSuggestions).mockResolvedValue(page([...others, item], 3));
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(3));
  const patch = deferred<SuggestionUpdateResult>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);
  act(() => void result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.unreadCount).toBe(2);

  // The Unread tab opens now; the server has applied the dismissal, so its
  // first page lacks the item and already counts two.
  vi.mocked(listSuggestions).mockResolvedValue(page(others, 2));
  const unread = renderHook(() => useProactiveInbox(true, ["unread"]), { wrapper });
  await waitFor(() => expect(unread.result.current.items).toEqual(others));
  expect(unread.result.current.unreadCount).toBe(2);
  expect(result.current.unreadCount).toBe(2);

  await act(async () =>
    patch.resolve(saved({ ...item, status: "dismissed", resolution: "not_now" }, 2)),
  );
  expect(unread.result.current.unreadCount).toBe(2);
  expect(result.current.unreadCount).toBe(2);
});

it.each(["succeeds", "fails"])(
  "moves a list's unread total even when its loaded pages stop before the item, and the PATCH %s",
  async (outcome) => {
    // The All tab's first page holds a newer read item; the older unread one is
    // behind it. The Unread tab's page holds the unread one.
    const newer: Suggestion = { ...item, id: "newer", status: "read", createdAt: 2 };
    vi.mocked(listSuggestions).mockImplementation(async (_cursor, statuses) =>
      statuses?.length === 1 ? page([item], 1) : { ...page([newer], 1), nextCursor: "1:sug" },
    );
    const { result } = renderHook(
      () => ({ all: useProactiveInbox(true), unread: useProactiveInbox(true, ["unread"]) }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.unread.items).toEqual([item]));
    await waitFor(() => expect(result.current.all.unreadCount).toBe(1));
    const patch = deferred<SuggestionUpdateResult>();
    vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

    // Expanding the item on the Unread tab marks it read.
    act(() => void result.current.unread.update("sug", { status: "read" }));
    expect(result.current.unread.unreadCount).toBe(0);
    expect(result.current.all.unreadCount).toBe(0);

    vi.mocked(listSuggestions).mockImplementation(() => new Promise(() => {}));
    if (outcome === "succeeds") {
      await act(async () => patch.resolve(saved({ ...item, status: "read" })));
      expect(result.current.all.unreadCount).toBe(0);
      expect(result.current.unread.unreadCount).toBe(0);
    } else {
      await act(async () => patch.reject(new Error("offline")));
      await waitFor(() => expect(showToast).toHaveBeenCalledOnce());
      // Both lists go back to one unread item, the one that never loaded included.
      expect(result.current.all.unreadCount).toBe(1);
      expect(result.current.unread.unreadCount).toBe(1);
      expect(result.current.unread.items).toEqual([item]);
    }
  },
);
