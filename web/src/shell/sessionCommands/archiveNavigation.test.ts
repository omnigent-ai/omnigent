import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";

import {
  type ArchiveNavigationRequest,
  hasPendingArchiveNavigation,
  leavingSessionOfOpenView,
  leaveSessionView,
  navigateAfterArchiving,
  newSessionPathFor,
  resetArchiveNavigationForTests,
} from "./archiveNavigation";

const LIVE = new Set(["a", "b", "c", "child"]);
let queryClient = new QueryClient();
const navigate = vi.fn();
let info: MockInstance<typeof console.info>;

/** The live sessions and visits ⌃[ walks, as the mounted recent-session host registers them. */
function registerSessions({
  history = ["a", "b"],
  order = ["c", "b", "a"],
  known = true,
  failed = false,
  open = "a",
}: {
  history?: string[];
  order?: string[];
  known?: boolean;
  failed?: boolean;
  open?: string;
} = {}) {
  useRecentSessionSwitchStore.setState({
    history,
    readLiveSessionIds: () => LIVE,
    sessionsKnown: known,
    sessionsFailed: failed,
  });
  useSidebarSessionOrderStore.setState({
    published: true,
    orderedSessionIds: order,
    activeSessionId: open,
    pendingSessionId: null,
  });
}

function archive(overrides: Partial<ArchiveNavigationRequest> = {}) {
  navigateAfterArchiving({
    leavingIds: ["a"],
    source: "palette",
    navigate,
    newSessionPath: "/?project=Payments",
    queryClient,
    ...overrides,
  });
}

const landed = () =>
  info.mock.calls
    .filter(([tag]) => tag === "[omnigent] session archive navigation")
    .map(([, record]) => (record as { landed: string }).landed);

beforeEach(() => {
  info = vi.spyOn(console, "info").mockImplementation(() => undefined);
});

afterEach(() => {
  resetArchiveNavigationForTests();
  navigate.mockClear();
  info.mockRestore();
  queryClient = new QueryClient();
  useRecentSessionSwitchStore.setState({
    history: [],
    readLiveSessionIds: null,
    sessionsKnown: false,
    sessionsFailed: false,
  });
  useSidebarSessionOrderStore.setState({
    published: false,
    orderedSessionIds: [],
    activeSessionId: null,
    pendingSessionId: null,
  });
});

describe("navigateAfterArchiving", () => {
  it("goes home, as before, when no recent-session host is mounted", () => {
    archive();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/", { replace: true });
    expect(landed()).toEqual([]);
  });

  it("opens the session visited before it, as ⌃[ would, and steps on from there", () => {
    registerSessions();
    archive();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/b", { replace: true });
    expect(useSidebarSessionOrderStore.getState().pendingSessionId).toBe("b");
    expect(info).toHaveBeenCalledWith("[omnigent] session archive navigation", {
      event: "session_archive_navigation",
      source: "palette",
      landed: "recent",
    });
  });

  it("falls back to sidebar order, then to New session in the archived session's project", () => {
    registerSessions({ history: ["a"] });
    archive();
    expect(navigate).toHaveBeenLastCalledWith("/c/c", { replace: true });

    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => new Set(["a"]) });
    archive();
    expect(navigate).toHaveBeenLastCalledWith("/?project=Payments", { replace: true });
    expect(landed()).toEqual(["sidebar_order", "new_session"]);
  });

  it("passes over a sub-agent of the archived session", () => {
    queryClient.setQueryData(["session", "child"], { parentSessionId: "a" });
    registerSessions({ history: ["child", "a", "b"], open: "child" });
    archive();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/b", { replace: true });
  });

  it("waits for a list that is still loading, then opens the survivor, never New session", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    expect(navigate).not.toHaveBeenCalled();
    expect(hasPendingArchiveNavigation()).toBe(true);

    useRecentSessionSwitchStore.setState({ sessionsKnown: true });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/c", { replace: true });
    expect(hasPendingArchiveNavigation()).toBe(false);
    expect(landed()).toEqual(["pending", "sidebar_order"]);
  });

  it("drops the wait for good once the user opens another session, even if they come back", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    useSidebarSessionOrderStore.setState({ activeSessionId: "b" });
    useSidebarSessionOrderStore.setState({ activeSessionId: "a" });
    useRecentSessionSwitchStore.setState({ sessionsKnown: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(landed()).toEqual(["pending", "user_left"]);
  });

  it("keeps waiting while the archived row drops out of the list, which still shows it open", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    // The archive patches the row out of the sidebar's list; the route still shows the session.
    useSidebarSessionOrderStore.setState({ orderedSessionIds: ["c", "b"] });
    useRecentSessionSwitchStore.setState({ sessionsKnown: true });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/c", { replace: true });
    expect(landed()).toEqual(["pending", "sidebar_order"]);
  });

  it("keeps waiting while the sidebar stops publishing, as when it unmounts", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    useSidebarSessionOrderStore.setState({
      published: false,
      orderedSessionIds: [],
      activeSessionId: null,
    });
    useRecentSessionSwitchStore.setState({ sessionsKnown: true });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/?project=Payments", { replace: true });
    expect(landed()).toEqual(["pending", "new_session"]);
  });

  it("records a list that fails while it waits, once, then opens the survivor on retry", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    useRecentSessionSwitchStore.setState({ sessionsKnown: true, sessionsFailed: true });
    // A refetch that fails again changes the reader, not the outcome.
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => LIVE });
    expect(navigate).not.toHaveBeenCalled();

    useRecentSessionSwitchStore.setState({ sessionsFailed: false });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/c", { replace: true });
    expect(landed()).toEqual(["pending", "list_failed", "sidebar_order"]);
  });

  it("drops the wait when the host that registered the list unmounts", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: null });
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => LIVE, sessionsKnown: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(hasPendingArchiveNavigation()).toBe(false);
  });

  it("lets a later archive replace a choice still waiting", () => {
    registerSessions({ history: ["a"], known: false });
    archive();
    archive({ leavingIds: ["a", "c"], source: "bulk" });
    useRecentSessionSwitchStore.setState({ sessionsKnown: true });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/b", { replace: true });
  });
});

describe("leaveSessionView", () => {
  const conversation = { id: "a", labels: { omni_project: "Payments" }, project_id: null };

  it("starts New session in a filed session's project when it was the last one live", () => {
    // Filing sets project_id and clears the project label.
    queryClient.setQueryData(["projects"], [{ id: "p-7", name: "Q3 plans", icon: null }]);
    registerSessions();
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => new Set(["a"]) });
    leaveSessionView("archive", {
      conversation: { id: "a", labels: {}, project_id: "p-7" },
      source: "header_menu",
      navigate,
      queryClient,
    });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/?project=Q3%20plans", { replace: true });
  });

  it("opens the session ⌃[ would after an archive", () => {
    registerSessions();
    leaveSessionView("archive", { conversation, source: "sidebar_menu", navigate, queryClient });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/c/b", { replace: true });
  });

  it("goes home after a delete or a leave", () => {
    registerSessions();
    leaveSessionView("delete", { conversation, source: "palette", navigate, queryClient });
    leaveSessionView("leave", { conversation, source: "palette", navigate, queryClient });
    expect(navigate.mock.calls).toEqual([
      ["/", { replace: true }],
      ["/", { replace: true }],
    ]);
  });
});

describe("helpers", () => {
  it("starts a new session in the archived session's project, by id or by its older label", () => {
    queryClient.setQueryData(["projects"], [{ id: "p-7", name: "Q3 plans", icon: null }]);
    expect(newSessionPathFor(queryClient, { labels: {}, project_id: "p-7" })).toBe(
      "/?project=Q3%20plans",
    );
    expect(
      newSessionPathFor(queryClient, { labels: { omni_project: "Payments" }, project_id: null }),
    ).toBe("/?project=Payments");
    expect(newSessionPathFor(queryClient, { labels: {}, project_id: null })).toBe("/");
  });

  it("falls back to the label, then home, for a project the list doesn't have", () => {
    queryClient.setQueryData(["projects"], []);
    expect(
      newSessionPathFor(queryClient, { labels: { omni_project: "Payments" }, project_id: "p-9" }),
    ).toBe("/?project=Payments");
    expect(newSessionPathFor(queryClient, { labels: {}, project_id: "p-9" })).toBe("/");
  });

  it("names the archived session the open one leaves with: itself, else its nearest ancestor", () => {
    queryClient.setQueryData(["session", "child"], { parentSessionId: "a" });
    queryClient.setQueryData(["session", "a"], { parentSessionId: "root" });
    expect(leavingSessionOfOpenView(queryClient, "child", ["b", "root", "a"])).toBe("a");
    expect(leavingSessionOfOpenView(queryClient, "child", ["b", "root"])).toBe("root");
    expect(leavingSessionOfOpenView(queryClient, "child", ["child", "a"])).toBe("child");
    expect(leavingSessionOfOpenView(queryClient, "b", ["a"])).toBeNull();
    expect(leavingSessionOfOpenView(queryClient, null, ["a"])).toBeNull();
  });
});
