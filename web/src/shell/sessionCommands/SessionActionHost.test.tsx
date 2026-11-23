import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";
import type * as SessionModule from "@/hooks/useSession";
import type * as UnseenModule from "@/hooks/useUnseenConversations";
import type * as PermissionsApiModule from "@/lib/permissionsApi";
import { LEVEL_READ } from "@/lib/permissionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";

import { SessionActionHost } from "./SessionActionHost";
import { usePublishOpenSession } from "../openSession";
import { requestSessionAction, useSessionActionStore } from "./sessionActionStore";

const writes = vi.hoisted(() => ({
  pin: vi.fn(),
  mute: vi.fn(),
  move: vi.fn(),
  rename: vi.fn(),
  archive: vi.fn(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  const mutation = (mutate: (...args: unknown[]) => void) => () => ({
    mutate,
    mutateAsync: vi.fn(async () => undefined),
    isPending: false,
  });
  return {
    ...actual,
    useProjects: () => ({ data: [{ id: "p1", name: "Payments", icon: null }] }),
    useTogglePinnedConversation: mutation(writes.pin),
    useSetConversationMuted: mutation(writes.mute),
    useMoveToProject: mutation(writes.move),
    useRenameConversation: mutation(writes.rename),
    useArchiveConversation: mutation(writes.archive),
  };
});
// The server's answers to Stop and Leave, which each test settles when it chooses.
const server = vi.hoisted(() => ({ stopSession: vi.fn(), revokePermission: vi.fn() }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  stopSession: server.stopSession,
}));
vi.mock("@/lib/permissionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof PermissionsApiModule>()),
  revokePermission: server.revokePermission,
}));
const ui = vi.hoisted(() => ({ toast: vi.fn(), viewing: null as string | null }));
vi.mock("@/components/ui/toast", () => ({ showToast: ui.toast }));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionModule>()),
  useActiveRootSessionId: () => ui.viewing,
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "viewer" }));
const readMarks = vi.hoisted(() => ({ unread: vi.fn(), read: vi.fn() }));
vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UnseenModule>()),
  // Seen, so its row offers Mark as unread.
  useConversationReadState: () => ({ unseen: false, explicitlyUnread: false }),
  markConversationUnread: readMarks.unread,
  markConversationRead: readMarks.read,
}));
vi.mock("@/hooks/useSessionActionRestrictions", () => ({
  useSessionActionRestrictions: () => ({ forkDisabledReason: undefined }),
}));

const SESSION: Conversation = {
  id: "c1",
  object: "conversation",
  title: "Fix the parser",
  created_at: 0,
  updated_at: 100,
  labels: {},
  permission_level: null,
  owner: "viewer",
};

function Location() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

/** Publishes the open session as the app shell does; its root comes from `ui.viewing`. */
function OpenSessionShell() {
  usePublishOpenSession(ui.viewing);
  return null;
}

/** On the open session's page; `remountHost` mounts the host again in the same app. */
function renderHost(queryClient = new QueryClient()) {
  const app = (hostMounted: boolean) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/c/c1"]}>
        <OpenSessionShell />
        {hostMounted && <SessionActionHost />}
        <Location />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { rerender } = render(app(true));
  return {
    remountHost: () => {
      rerender(app(false));
      rerender(app(true));
    },
  };
}
const location = () => screen.getByTestId("location").textContent;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useSessionActionStore.setState({ request: null });
  ui.viewing = null;
});

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((settle, fail) => {
    resolve = () => settle();
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe("SessionActionHost", () => {
  it("pins through the row menu's write", () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "pin"));
    expect(writes.pin).toHaveBeenCalledWith({ id: "c1", pinned: true });
  });

  it("reads a family pinned through its leader as pinned, as its row does", () => {
    renderHost();
    const family = { id: "f1", representative_id: "c1", pinned: true };
    act(() => requestSessionAction({ ...SESSION, family }, "pin"));
    expect(writes.pin).not.toHaveBeenCalled();
  });

  it("marks the session unread as its row does", () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "mark-unread"));
    expect(readMarks.unread).toHaveBeenCalledWith("c1", 100);
    // Already read, so Mark as read doesn't apply.
    act(() => requestSessionAction(SESSION, "mark-read"));
    expect(readMarks.read).not.toHaveBeenCalled();
  });

  it("asks nothing of the server for a session still being created", () => {
    renderHost();
    const draft: Conversation = { ...SESSION, id: "temp:example", provisional: true };
    act(() => requestSessionAction(draft, "pin"));
    act(() => requestSessionAction(draft, "rename"));
    expect(writes.pin).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("does nothing for an action that stopped applying, such as pinning it once archived", () => {
    renderHost();
    act(() => requestSessionAction({ ...SESSION, archived: true }, "pin"));
    act(() => requestSessionAction({ ...SESSION, owner: "someone-else" }, "rename"));
    expect(writes.pin).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renames from its dialog", async () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "rename"));
    const field = await screen.findByTestId("rename-session-input");
    fireEvent.change(field, { target: { value: "Parser rewrite" } });
    fireEvent.submit(field.closest("form")!);
    expect(writes.rename).toHaveBeenCalledWith({ id: "c1", title: "Parser rewrite" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("files the session from the project picker", async () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "move-to-project"));
    const input = await screen.findByRole("combobox", { name: "Add session to a project" });
    fireEvent.change(input, { target: { value: "payments" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(writes.move).toHaveBeenCalledWith({ id: "c1", project: "Payments" });
  });

  it("asks before deleting, with the row menu's dialog", async () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "delete"));
    expect(await screen.findByRole("dialog", { name: "Delete session?" })).toBeTruthy();
  });

  it("starts a second request in place of the first", async () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "delete"));
    await screen.findByRole("dialog", { name: "Delete session?" });
    act(() => requestSessionAction(SESSION, "stop"));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Delete session?" })).toBeNull(),
    );
  });

  it("archives a request queued before the host mounted, and leaves the open session's view", () => {
    ui.viewing = "c1";
    requestSessionAction(SESSION, "archive");
    renderHost();
    expect(writes.archive).toHaveBeenCalledWith({
      id: "c1",
      archived: true,
      deleteWorktree: false,
    });
    expect(location()).toBe("/");
  });

  it("runs an action once, even on a host mounted again", () => {
    const { remountHost } = renderHost();
    act(() => requestSessionAction(SESSION, "pin"));
    remountHost();
    expect(writes.pin).toHaveBeenCalledTimes(1);
  });

  describe("reports a Stop or Leave however the host changed while it saved", () => {
    const RUNNING: Conversation = { ...SESSION, host_id: "h1", runner_id: "r1" };
    const SHARED: Conversation = { ...SESSION, owner: "someone-else" };

    async function startStop() {
      const stop = deferred();
      server.stopSession.mockReturnValue(stop.promise);
      const host = renderHost();
      act(() => requestSessionAction(RUNNING, "stop"));
      fireEvent.click(await screen.findByTestId("stop-session-confirm"));
      await waitFor(() => expect(server.stopSession).toHaveBeenCalledWith("c1"));
      return { stop, ...host };
    }

    async function startLeave() {
      const leave = deferred();
      server.revokePermission.mockReturnValue(leave.promise);
      ui.viewing = "c1";
      const host = renderHost();
      act(() => requestSessionAction(SHARED, "leave"));
      fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
      await waitFor(() => expect(server.revokePermission).toHaveBeenCalledWith("c1", "viewer"));
      return { leave, ...host };
    }

    it("shows a failed Stop's error after another action replaced it", async () => {
      const { stop } = await startStop();
      act(() => requestSessionAction(SESSION, "mark-unread"));
      await act(async () => stop.reject(new Error("runner unreachable")));
      expect(ui.toast).toHaveBeenCalledWith("Couldn't stop the session: runner unreachable");
    });

    it("shows a failed Stop's error after the host mounted again, stopping once", async () => {
      const { stop, remountHost } = await startStop();
      remountHost();
      await act(async () => stop.reject(new Error("runner unreachable")));
      expect(ui.toast).toHaveBeenCalledWith("Couldn't stop the session: runner unreachable");
      expect(server.stopSession).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("says nothing when the Stop succeeds after the host mounted again", async () => {
      const { stop, remountHost } = await startStop();
      remountHost();
      await act(async () => stop.resolve());
      expect(ui.toast).not.toHaveBeenCalled();
    });

    it("leaves the open session after Leave succeeds, though another action replaced it", async () => {
      const { leave } = await startLeave();
      act(() => requestSessionAction(SESSION, "mark-unread"));
      await act(async () => leave.resolve());
      expect(location()).toBe("/");
    });

    it("leaves the open session after Leave succeeds, though the host mounted again", async () => {
      const { leave, remountHost } = await startLeave();
      remountHost();
      await act(async () => leave.resolve());
      expect(location()).toBe("/");
      expect(server.revokePermission).toHaveBeenCalledTimes(1);
    });

    it("stays and says why when Leave fails after the host mounted again", async () => {
      const { leave, remountHost } = await startLeave();
      remountHost();
      await act(async () => leave.reject(new Error("offline")));
      expect(ui.toast).toHaveBeenCalledWith("Couldn't leave the session: offline");
      expect(location()).toBe("/c/c1");
    });

    it("stays on another session opened before Leave succeeds", async () => {
      const { leave, remountHost } = await startLeave();
      ui.viewing = "c2";
      remountHost();
      await act(async () => leave.resolve());
      expect(location()).toBe("/c/c1");
    });
  });

  describe("looks the session up as the action starts", () => {
    const cachedSessions = (...rows: Conversation[]) => {
      const queryClient = new QueryClient();
      queryClient.setQueryData(["conversations", "sidebar"], {
        pages: [{ data: rows }],
        pageParams: [undefined],
      });
      return queryClient;
    };

    it("does nothing once the cache marks a listed session archived", () => {
      renderHost(cachedSessions({ ...SESSION, archived: true }));
      // The palette asked with the row it showed before the archive.
      act(() => requestSessionAction(SESSION, "pin", { listed: true }));
      expect(writes.pin).not.toHaveBeenCalled();
    });

    it("does nothing once a listed session has left every list, even after it returns", () => {
      const other: Conversation = { ...SESSION, id: "c2" };
      const queryClient = cachedSessions();
      const { remountHost } = renderHost(queryClient);
      act(() => requestSessionAction(other, "mark-unread", { listed: true }));
      expect(readMarks.unread).not.toHaveBeenCalled();

      // A refetch lists it again before the host mounts once more.
      queryClient.setQueryData(["conversations", "sidebar"], {
        pages: [{ data: [other] }],
        pageParams: [undefined],
      });
      remountHost();
      expect(readMarks.unread).not.toHaveBeenCalled();
    });

    it("checks the current owner, not the one the row showed", () => {
      renderHost(cachedSessions({ ...SESSION, owner: "someone-else" }));
      act(() => requestSessionAction(SESSION, "rename", { listed: true }));
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("checks a search result's owner, not a fetched row that can't say whose it is", () => {
      const queryClient = cachedSessions();
      queryClient.setQueryData(["conversation-backfill", "c1"], {
        ...SESSION,
        owner: null,
        permission_level: LEVEL_READ,
      });
      renderHost(queryClient);
      act(() =>
        requestSessionAction(
          { ...SESSION, owner: "someone-else", permission_level: LEVEL_READ },
          "rename",
        ),
      );
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("acts on the current row, and on a search result no list holds", () => {
      renderHost(cachedSessions({ ...SESSION, updated_at: 200 }));
      act(() => requestSessionAction(SESSION, "mark-unread", { listed: true }));
      expect(readMarks.unread).toHaveBeenCalledWith("c1", 200);
      const found: Conversation = { ...SESSION, id: "c9" };
      act(() => requestSessionAction(found, "pin"));
      expect(writes.pin).toHaveBeenCalledWith({ id: "c9", pinned: true });
    });
  });
});
