import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { MouseEvent, ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { toast } from "sonner";
import * as identity from "@/lib/identity";
import { clearRecentlyCreated, recentlyCreatedSessions } from "@/lib/sessionListCache";
import { clearSessionTombstones } from "./useConversations";
import { childSessionsQueryKey, type ChildSessionInfo } from "./useChildSessions";
import { useArchiveRelatedSessions } from "./useRelatedSessionLifecycle";

vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));

afterEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  clearRecentlyCreated();
  clearSessionTombstones();
});

it.each([null, "root"])(
  "Undo restores only changed sessions without promoting peers (parent %s)",
  async (parent) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const id = parent ? "peer" : "root";
    const changed = [id, "helper"];
    const snapshot = {
      id,
      parent_session_id: parent,
      labels: {},
      archive_changed_session_ids: changed,
    };
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...snapshot, archived: true })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...snapshot, archived: false })));
    client.setQueryData(["session", id], { id, archived: false });
    client.setQueryData(childSessionsQueryKey(id), [
      { id: "helper", archived: false },
      { id: "previously-archived", archived: true },
    ]);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useArchiveRelatedSessions(), { wrapper });
    await act(() => result.current.mutateAsync({ conversation: { id }, archived: true }));
    expect(JSON.parse(fetch.mock.calls[0][1]?.body as string)).toEqual({
      archived: true,
      archive_scope: "tree",
    });
    expect(client.getQueryData(["session", id])).toMatchObject({ archived: true });
    const options = vi.mocked(toast).mock.calls[0][1];
    expect(options?.action).toMatchObject({ label: "Undo" });
    const undo = options?.action as { onClick: (event: MouseEvent<HTMLButtonElement>) => void };
    act(() => undo.onClick({} as MouseEvent<HTMLButtonElement>));
    await waitFor(() =>
      expect(client.getQueryData(["session", id])).toMatchObject({ archived: false }),
    );
    expect(JSON.parse(fetch.mock.calls[1][1]?.body as string)).toEqual({
      archived: false,
      archive_scope: "tree",
      archive_session_ids: changed,
    });
    expect(
      client
        .getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(id))
        ?.map((row) => row.archived),
    ).toEqual([false, true]);
    expect(recentlyCreatedSessions.has(id)).toBe(parent === null);
    expect(recentlyCreatedSessions.has("helper")).toBe(false);
    client.clear();
  },
);

it("does not offer Undo when the group mutation fails", async () => {
  const client = new QueryClient();
  vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
    new Response("failure", { status: 500 }),
  );
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useArchiveRelatedSessions(), { wrapper });
  await act(async () => {
    await expect(
      result.current.mutateAsync({ conversation: { id: "root" }, archived: true }),
    ).rejects.toThrow();
  });
  expect(toast).not.toHaveBeenCalled();
  expect(toast.error).toHaveBeenCalled();
  client.clear();
});
