import { act, renderHook } from "@testing-library/react";
import type { ContextType, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { SidebarDataContext } from "@/hooks/useSidebarData";
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

type SidebarData = ContextType<typeof SidebarDataContext>;
const loaded = (rows: Conversation[]) => ({ loadedRows: rows }) as unknown as SidebarData;
const SEVERAL_LOADED = loaded([row("a", 1), row("b", 9), row("c", 5)]);
const ONLY_OPEN_LOADED = loaded([row("b", 9)]);
/** The sidebar's loaded rows, as the shell provides them. */
const withRows = (sidebarData: SidebarData) => ({
  wrapper: ({ children }: { children: ReactNode }) => (
    <SidebarDataContext.Provider value={sidebarData}>{children}</SidebarDataContext.Provider>
  ),
});

afterEach(() => {
  navigate.mockClear();
  useSidebarSessionOrderStore.setState({ orderedSessionIds: [], activeSessionId: null });
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
    command(result.current, "previous-session").run();
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
  });

  it("offers no steps while no sidebar lists sessions", () => {
    const { result } = renderHook(() => useSessionNavigationCommands());
    expect(isCommandListed(command(result.current, "next-session").availability)).toBe(false);
    expect(isCommandListed(command(result.current, "previous-session").availability)).toBe(false);
  });

  it("switches to the most recent other session, and offers none without one", () => {
    const { result } = renderHook(() => useSessionNavigationCommands(), withRows(SEVERAL_LOADED));
    command(result.current, "recent-session").run();
    expect(navigate).toHaveBeenCalledWith("/c/c");

    const alone = renderHook(() => useSessionNavigationCommands(), withRows(ONLY_OPEN_LOADED));
    expect(isCommandListed(command(alone.result.current, "recent-session").availability)).toBe(
      false,
    );
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
