import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

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
});
