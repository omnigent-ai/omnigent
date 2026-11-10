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
  state: "unread",
  resolution: "none",
  createdAt: 1,
};

function page(data: Suggestion[], unreadCount: number): SuggestionPage {
  return { data, nextCursor: null, unreadCount, unreadCountCapped: false };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
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
  const patch = deferred<Suggestion>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

  act(() => result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);

  await act(async () => stalePoll.resolve(page([item], 1)));
  expect(result.current.items).toEqual([]);

  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () => patch.resolve({ ...item, state: "dismissed", resolution: "not_now" }));
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
  const read = deferred<Suggestion>();
  const dismiss = deferred<Suggestion>();
  vi.mocked(updateSuggestion)
    .mockReturnValueOnce(read.promise)
    .mockReturnValueOnce(dismiss.promise);

  // Expanding marks the item read; the viewer dismisses it before that settles.
  act(() => result.current.update("sug", { state: "read" }));
  act(() => result.current.update("sug", { resolution: "not_relevant" }));
  expect(result.current.items).toEqual([]);
  // The dismissal waits for the read, so the server sees them in order.
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(updateSuggestion).toHaveBeenCalledTimes(1);

  // The read's response says the item is open; it must not bring it back.
  await act(async () => read.resolve({ ...item, state: "read" }));
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledTimes(2));
  expect(vi.mocked(updateSuggestion).mock.calls[1]).toEqual([
    "sug",
    { resolution: "not_relevant" },
  ]);
  expect(result.current.items).toEqual([]);

  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () =>
    dismiss.resolve({ ...item, state: "dismissed", resolution: "not_relevant" }),
  );
  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(2));
  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);
});

it("ignores a read mark on a suggestion the viewer already resolved", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  const dismiss = deferred<Suggestion>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(dismiss.promise);

  act(() => result.current.update("sug", { resolution: "already_done" }));
  act(() => result.current.update("sug", { state: "read" }));
  vi.mocked(listSuggestions).mockResolvedValue(page([], 0));
  await act(async () => dismiss.resolve({ ...item, state: "done", resolution: "already_done" }));

  await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(2));
  expect(updateSuggestion).toHaveBeenCalledTimes(1);
  expect(result.current.items).toEqual([]);
});

it("reports a failed update and reloads the list", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));
  vi.mocked(updateSuggestion).mockRejectedValueOnce(new Error("offline"));

  act(() => result.current.update("sug", { resolution: "not_now" }));
  expect(result.current.items).toEqual([]);

  await waitFor(() => expect(showToast).toHaveBeenCalledOnce());
  // The server still has the item open, so the refetch shows it again.
  await waitFor(() => expect(result.current.items).toEqual([item]));
  expect(listSuggestions).toHaveBeenCalledTimes(2);
});

it("lists only the requested states after a read mark", async () => {
  vi.mocked(updateSuggestion).mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useProactiveInbox(true, ["unread"]), { wrapper });
  await waitFor(() => expect(result.current.items).toEqual([item]));

  act(() => result.current.update("sug", { state: "read" }));

  expect(result.current.items).toEqual([]);
});

it.each(["fails", "hangs"])(
  "keeps a dismissal when a poll started during the PATCH carries the old item and the refetch %s",
  async (refetchOutcome) => {
    const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
    await waitFor(() => expect(result.current.unreadCount).toBe(1));
    const patch = deferred<Suggestion>();
    vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);

    act(() => result.current.update("sug", { resolution: "not_now" }));
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
    await act(async () => patch.resolve({ ...item, state: "dismissed", resolution: "not_now" }));
    await waitFor(() => expect(listSuggestions).toHaveBeenCalledTimes(3));

    expect(result.current.items).toEqual([]);
    expect(result.current.unreadCount).toBe(0);
  },
);

it("drops a poll still in flight when the PATCH settles", async () => {
  const { result } = renderHook(() => useProactiveInbox(true), { wrapper });
  await waitFor(() => expect(result.current.unreadCount).toBe(1));
  const patch = deferred<Suggestion>();
  vi.mocked(updateSuggestion).mockReturnValueOnce(patch.promise);
  act(() => result.current.update("sug", { resolution: "not_now" }));
  await waitFor(() => expect(updateSuggestion).toHaveBeenCalledOnce());
  const stalePoll = deferred<SuggestionPage>();
  vi.mocked(listSuggestions).mockReturnValueOnce(stalePoll.promise);
  void act(() => void result.current.refetch());

  vi.mocked(listSuggestions).mockReturnValue(new Promise(() => {}));
  await act(async () => patch.resolve({ ...item, state: "dismissed", resolution: "not_now" }));
  await act(async () => stalePoll.resolve(page([item], 1)));

  expect(result.current.items).toEqual([]);
  expect(result.current.unreadCount).toBe(0);
});
