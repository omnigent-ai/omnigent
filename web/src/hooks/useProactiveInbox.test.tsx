import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
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

let client: QueryClient;
let enabled = true;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, proactive_agent_enabled: enabled }}>
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
  chart: null,
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
