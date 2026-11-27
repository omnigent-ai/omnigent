// Where an archive lands, against the real router and the sidebar's real order publisher.
import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import {
  usePublishSidebarSessionOrder,
  useSidebarSessionOrderStore,
} from "@/hooks/useSidebarSessionOrder";

import { navigateAfterArchiving, resetArchiveNavigationForTests } from "./archiveNavigation";

const NO_ROWS: readonly string[] = [];
const app = { pathname: "", navigate: (_path: string, _options?: { replace?: boolean }) => {} };

/** A mounted sidebar whose list is still loading: no rows, and the routed session as open. */
function LoadingSidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  app.pathname = location.pathname;
  app.navigate = navigate;
  const routed = /^\/c\/([^/]+)$/.exec(location.pathname)?.[1] ?? null;
  usePublishSidebarSessionOrder(NO_ROWS, routed);
  return null;
}

afterEach(() => {
  cleanup();
  resetArchiveNavigationForTests();
  useRecentSessionSwitchStore.setState({
    history: [],
    readLiveSessionIds: null,
    sessionsKnown: false,
    sessionsFailed: false,
  });
});

describe("navigateAfterArchiving with the real router", () => {
  it("leaves the user on Inbox when they go there while the list loads, though no rows show", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    render(
      <MemoryRouter initialEntries={["/c/a"]}>
        <LoadingSidebar />
      </MemoryRouter>,
    );
    useRecentSessionSwitchStore.setState({
      // Only the archived session was visited, so the landing waits for the list.
      history: ["a"],
      readLiveSessionIds: () => new Set(["a", "b"]),
      sessionsKnown: false,
    });
    navigateAfterArchiving({
      leavingIds: ["a"],
      source: "palette",
      navigate: (path, options) => app.navigate(path, options),
      newSessionPath: "/",
      queryClient: new QueryClient(),
    });

    // Inbox shows no session, and the loading sidebar still lists none.
    act(() => app.navigate("/inbox"));
    expect(useSidebarSessionOrderStore.getState()).toMatchObject({
      orderedSessionIds: [],
      activeSessionId: null,
    });
    act(() => useRecentSessionSwitchStore.setState({ sessionsKnown: true }));

    expect(app.pathname).toBe("/inbox");
    expect(info.mock.calls.map(([, record]) => (record as { landed: string }).landed)).toEqual([
      "pending",
      "user_left",
    ]);
    info.mockRestore();
  });
});
