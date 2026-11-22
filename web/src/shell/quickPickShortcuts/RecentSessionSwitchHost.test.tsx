import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import { SidebarDataContext } from "@/hooks/useSidebarData";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { conversation } from "@/test/sidebarMockHelpers";

import { RecentSessionSwitchHost } from "./RecentSessionSwitchHost";

vi.mock("../RecentSessionSwitcher", () => ({ RecentSessionSwitcher: () => null }));

interface Mine {
  data: unknown;
  isError: boolean;
}

function renderHost(mine: Mine) {
  const client = new QueryClient();
  client.setQueryData(["conversations", "", false, null, "mine"], {
    pages: [
      {
        data: [conversation("a"), conversation("b"), conversation("gone", { archived: true })],
        first_id: null,
        last_id: null,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  });
  const wrap = (current: Mine): ReactNode => sidebarWith(client, { mine: current } as never);
  const view = render(wrap(mine));
  return { rerender: (next: Mine) => view.rerender(wrap(next)) };
}

function sidebarWith(client: QueryClient, sidebarData: never): ReactNode {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SidebarDataContext.Provider value={sidebarData}>
          <RecentSessionSwitchHost />
        </SidebarDataContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  useRecentSessionSwitchStore.setState({ history: [], sessionsKnown: false, switcher: null });
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: [],
    activeSessionId: null,
    pendingSessionId: null,
  });
});

describe("RecentSessionSwitchHost", () => {
  it("offers the cached live sessions once the first page has loaded or failed", () => {
    const { rerender } = renderHost({ data: undefined, isError: false });
    expect(useRecentSessionSwitchStore.getState().sessionsKnown).toBe(false);
    rerender({ data: { pages: [] }, isError: false });
    const state = useRecentSessionSwitchStore.getState();
    expect(state.sessionsKnown).toBe(true);
    expect([...(state.readLiveSessionIds?.() ?? [])].toSorted()).toEqual(["a", "b"]);
    rerender({ data: undefined, isError: true });
    expect(useRecentSessionSwitchStore.getState()).toMatchObject({
      sessionsKnown: true,
      sessionsFailed: true,
    });
  });

  it("records each session the sidebar opens as the latest visit", () => {
    renderHost({ data: { pages: [] }, isError: false });
    act(() => useSidebarSessionOrderStore.setState({ activeSessionId: "a" }));
    act(() => useSidebarSessionOrderStore.setState({ activeSessionId: "b" }));
    expect(useRecentSessionSwitchStore.getState().history.slice(0, 2)).toEqual(["b", "a"]);
  });
});
