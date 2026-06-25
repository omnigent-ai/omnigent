import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { childSessionsQueryKey, type ChildSessionInfo, useChildSessions } from "./useChildSessions";

function child(id: string): ChildSessionInfo {
  return {
    id,
    title: id,
    tool: "agent",
    session_name: id,
    current_task_status: null,
    busy: id === "a1",
    last_message_preview: null,
    pending_elicitations_count: 0,
  };
}

function renderWithTree(tree: Record<string, string[]>, includeDescendants = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const [sessionId, childIds] of Object.entries(tree)) {
    client.setQueryData(childSessionsQueryKey(sessionId), childIds.map(child));
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useChildSessions("root", null, includeDescendants), { wrapper });
}

afterEach(cleanup);

describe("useChildSessions", () => {
  it("keeps direct children as the default and includes descendants when requested", () => {
    const tree = {
      root: ["coord_a", "coord_b"],
      coord_a: ["a1", "a2"],
      coord_b: ["b1", "b2"],
      a1: ["deep"],
      a2: [],
      b1: [],
      b2: [],
      deep: ["too_deep"],
    };

    expect(renderWithTree(tree).result.current.children.map((item) => item.id)).toEqual([
      "coord_a",
      "coord_b",
    ]);
    expect(renderWithTree(tree, true).result.current.children.map((item) => item.id)).toEqual([
      "coord_a",
      "coord_b",
      "a1",
      "a2",
      "b1",
      "b2",
      "deep",
    ]);
    expect(
      renderWithTree(tree, true)
        .result.current.children.filter((item) => item.busy)
        .map((item) => item.id),
    ).toEqual(["a1"]);
  });
});
