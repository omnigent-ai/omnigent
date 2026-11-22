import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { isCommandListed } from "@/store/paletteCommands";

import { recentSessionId, useSessionNavigationCommands } from "./sessionNavigationCommands";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("./commandContext", () => ({ useCommandContext: () => ({ session: { id: "b" } }) }));

function row(id: string, updated: number, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: updated,
    labels: {},
    permission_level: null,
    ...overrides,
  };
}

afterEach(() => {
  navigate.mockClear();
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: [],
    activeSessionId: null,
    pendingSessionId: null,
  });
});

const command = (commands: ReturnType<typeof useSessionNavigationCommands>, id: string) =>
  commands.find((candidate) => candidate.id === id)!;

describe("useSessionNavigationCommands", () => {
  it("steps to the sidebar's next and previous rows, read when run", () => {
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["a", "b"], activeSessionId: "a" });
    const { result } = renderHook(() => useSessionNavigationCommands());
    // The list changed after the palette built its rows.
    act(() =>
      useSidebarSessionOrderStore.setState({
        orderedSessionIds: ["a", "b", "c"],
        activeSessionId: "b",
      }),
    );
    command(result.current, "next-session").run();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
    // Before the route renders c, a step back starts from it.
    command(result.current, "previous-session").run();
    expect(navigate).toHaveBeenLastCalledWith("/c/b");
  });

  it("offers no steps while no sidebar lists sessions", () => {
    const { result } = renderHook(() => useSessionNavigationCommands());
    expect(isCommandListed(command(result.current, "next-session").availability)).toBe(false);
    expect(isCommandListed(command(result.current, "previous-session").availability)).toBe(false);
  });

  it("stays put on a single listed session that's already open", () => {
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["a"], activeSessionId: "a" });
    const { result } = renderHook(() => useSessionNavigationCommands());
    command(result.current, "next-session").run();
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("recentSessionId", () => {
  it("picks the most recently active session other than the open one", () => {
    expect(recentSessionId([row("a", 1), row("b", 9), row("c", 5)], "b")).toBe("c");
  });

  it("skips archived and not-yet-created sessions, and has none to pick alone", () => {
    expect(
      recentSessionId(
        [row("a", 1), row("x", 9, { archived: true }), row("y", 8, { provisional: true })],
        null,
      ),
    ).toBe("a");
    expect(recentSessionId([row("b", 1)], "b")).toBeUndefined();
  });
});
