import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { LEVEL_OWNER, LEVEL_READ } from "@/lib/permissionsApi";

import { currentSessionRow, isSessionListed } from "./currentSession";

const SESSION: Conversation = {
  id: "c1",
  object: "conversation",
  title: "Fix the parser",
  created_at: 0,
  updated_at: 1,
  labels: {},
  permission_level: null,
};

function cached(...rows: Conversation[]) {
  const queryClient = new QueryClient();
  queryClient.setQueryData(["conversations", "sidebar"], {
    pages: [{ data: rows }],
    pageParams: [undefined],
  });
  return queryClient;
}

describe("currentSessionRow", () => {
  it("reads the cached row over the snapshot", () => {
    const queryClient = cached({ ...SESSION, title: "Parser rewrite" });
    expect(isSessionListed(queryClient, "c1")).toBe(true);
    expect(currentSessionRow(queryClient, SESSION, true)?.title).toBe("Parser rewrite");
  });

  it("knows a listed session that left the lists is gone, and keeps an unlisted one's snapshot", () => {
    const queryClient = cached();
    expect(isSessionListed(queryClient, "c1")).toBe(false);
    expect(currentSessionRow(queryClient, SESSION, true)).toBeNull();
    expect(currentSessionRow(queryClient, SESSION, false)).toBe(SESSION);
  });

  it("knows a session the server reported removed is gone", () => {
    const queryClient = cached();
    queryClient.setQueryData(["conversation-backfill", "c1"], null);
    expect(currentSessionRow(queryClient, SESSION, false)).toBeNull();
  });

  it("keeps a search result's own row when only the session's detail is cached", () => {
    const queryClient = cached();
    queryClient.setQueryData(["session", "c1"], { id: "c1", title: "Fix the parser" });
    const shared = { ...SESSION, owner: "someone-else" };
    // The detail carries no owner, so it never stands in for the row; reads stay the same object.
    expect(isSessionListed(queryClient, "c1")).toBe(false);
    expect(currentSessionRow(queryClient, shared, false)).toBe(shared);
    expect(currentSessionRow(queryClient, shared, false)).toBe(
      currentSessionRow(queryClient, shared, false),
    );
  });

  it("keeps a search result's owner over a fetched row that can't say whose session it is", () => {
    const queryClient = cached();
    // The open session's own fetch, shared with the viewer: no owner, below owner level.
    const fetched = { ...SESSION, owner: null, permission_level: LEVEL_READ };
    queryClient.setQueryData(["conversation-backfill", "c1"], fetched);
    const shared = { ...SESSION, owner: "someone-else", permission_level: LEVEL_READ };
    expect(isSessionListed(queryClient, "c1")).toBe(false);
    expect(currentSessionRow(queryClient, shared, false)).toBe(shared);
    expect(currentSessionRow(queryClient, shared, true)).toBeNull();
    // A fetched row the viewer owns at owner level still stands for the session.
    const owned = { ...fetched, permission_level: LEVEL_OWNER, title: "Parser rewrite" };
    queryClient.setQueryData(["conversation-backfill", "c1"], owned);
    expect(currentSessionRow(queryClient, shared, false)).toEqual(owned);
  });
});
