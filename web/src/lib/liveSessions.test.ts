import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { liveSessionIdsInCache, subscribeToSessionLists } from "./liveSessions";

const row = (id: string, archived = false) => ({ id, archived }) as Conversation;
const listPage = (rows: Conversation[]) => ({ pages: [{ data: rows }], pageParams: [null] });

describe("liveSessionIdsInCache", () => {
  it("reads every session list and pins, skipping archived rows", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["conversations", "mine"], listPage([row("a"), row("x", true)]));
    queryClient.setQueryData(["project-sessions", "p1"], listPage([row("b")]));
    queryClient.setQueryData(["pinned-conversations"], { conversations: [row("c")] });
    expect(liveSessionIdsInCache(queryClient)).toEqual(new Set(["a", "b", "c"]));
  });
});

describe("subscribeToSessionLists", () => {
  it("hears session-list changes only, not other queries, until unsubscribed", () => {
    const queryClient = new QueryClient();
    const onChange = vi.fn();
    const stop = subscribeToSessionLists(queryClient, onChange);

    queryClient.setQueryData(["messages", "a"], { items: [] });
    expect(onChange).not.toHaveBeenCalled();

    queryClient.setQueryData(["project-sessions", "p1"], listPage([row("b")]));
    const afterList = onChange.mock.calls.length;
    expect(afterList).toBeGreaterThan(0);
    queryClient.setQueryData(["pinned-conversations"], { conversations: [] });
    expect(onChange.mock.calls.length).toBeGreaterThan(afterList);

    stop();
    const afterStop = onChange.mock.calls.length;
    queryClient.setQueryData(["conversations", "mine"], listPage([]));
    expect(onChange).toHaveBeenCalledTimes(afterStop);
  });
});
