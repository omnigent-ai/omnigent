import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { liveSessionIdsInCache } from "@/lib/liveSessions";
import { isCommandListed } from "@/store/paletteCommands";

import { hasOtherLiveSession, useSessionNavigationCommands } from "./sessionNavigationCommands";

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

let queryClient = new QueryClient();
function WithQueryClient({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
const renderCommands = () =>
  renderHook(() => useSessionNavigationCommands(), { wrapper: WithQueryClient });

afterEach(() => {
  queryClient = new QueryClient();
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
    const { result } = renderCommands();
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
    const { result } = renderCommands();
    expect(isCommandListed(command(result.current, "next-session").availability)).toBe(false);
    expect(isCommandListed(command(result.current, "previous-session").availability)).toBe(false);
  });

  it("stays put on a single listed session that's already open", () => {
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["a"], activeSessionId: "a" });
    const { result } = renderCommands();
    command(result.current, "next-session").run();
    expect(navigate).not.toHaveBeenCalled();
  });
});

/** One page of a cached session list, as the sidebar's and folders' queries hold them. */
const listPage = (rows: Conversation[]) => ({ pages: [{ data: rows }], pageParams: [null] });

describe("Switch to recent session", () => {
  afterEach(() =>
    useRecentSessionSwitchStore.setState({
      switcher: null,
      history: [],
      readLiveSessionIds: null,
      sessionsKnown: false,
    }),
  );

  /** The switcher reads the cached lists, as the mounted host registers them. */
  function registerCachedSessions() {
    useRecentSessionSwitchStore.setState({
      readLiveSessionIds: () => liveSessionIdsInCache(queryClient),
      sessionsKnown: true,
    });
  }

  it("opens the switcher one visit back, without the Control hold, once the palette closes", () => {
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["a", "b"], activeSessionId: "b" });
    queryClient.setQueryData(["conversations", "mine"], listPage([row("a", 1), row("b", 2)]));
    registerCachedSessions();
    useRecentSessionSwitchStore.setState({ history: ["b", "a"] });
    const { result } = renderCommands();
    const recent = command(result.current, "recent-session");
    expect(recent.shortcut).toBe("session.recent");
    expect(recent.movesFocus).toBe(true);
    act(() => recent.run());
    expect(useRecentSessionSwitchStore.getState().switcher).toMatchObject({
      holding: false,
      selectedId: "a",
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("is offered only for a session the switcher can reach: visited, or in the sidebar's rows", () => {
    // The sidebar lists only the open session; the other sits in a project folder's list.
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["b"], activeSessionId: "b" });
    queryClient.setQueryData(["conversations", "mine"], listPage([row("b", 2)]));
    registerCachedSessions();
    const { result } = renderCommands();
    act(() => queryClient.setQueryData(["project-sessions", "p1"], listPage([row("z", 1)])));
    // Never visited and not in the sidebar's order, so the switcher would only reselect b.
    expect(isCommandListed(command(result.current, "recent-session").availability)).toBe(false);

    act(() => useRecentSessionSwitchStore.setState({ history: ["z"] }));
    expect(isCommandListed(command(result.current, "recent-session").availability)).toBe(true);
    act(() => useRecentSessionSwitchStore.setState({ history: [] }));
    act(() => useSidebarSessionOrderStore.setState({ orderedSessionIds: ["b", "z"] }));
    expect(isCommandListed(command(result.current, "recent-session").availability)).toBe(true);
  });

  it("isn't offered with no recent-session host, or none but the open session live", () => {
    const { result } = renderCommands();
    expect(isCommandListed(command(result.current, "recent-session").availability)).toBe(false);

    queryClient.setQueryData(
      ["conversations", "mine"],
      listPage([row("b", 2), row("x", 9, { archived: true })]),
    );
    act(registerCachedSessions);
    expect(isCommandListed(command(result.current, "recent-session").availability)).toBe(false);
  });
});

describe("hasOtherLiveSession", () => {
  it("looks past the open session and its sub-agent's root", () => {
    expect(hasOtherLiveSession(new Set(["a", "b"]), ["b", null])).toBe(true);
    expect(hasOtherLiveSession(new Set(["root"]), ["child", "root"])).toBe(false);
    expect(hasOtherLiveSession(new Set(), [null, null])).toBe(false);
  });
});
