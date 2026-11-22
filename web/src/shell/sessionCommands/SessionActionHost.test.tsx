import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";
import type * as SessionModule from "@/hooks/useSession";
import type * as UnseenModule from "@/hooks/useUnseenConversations";
import type * as PermissionsApiModule from "@/lib/permissionsApi";
import type * as RoutingModule from "@/lib/routing";
import type * as SessionsApiModule from "@/lib/sessionsApi";

import { SessionActionHost } from "./SessionActionHost";
import {
  latestSessionActionRequest,
  requestSessionAction,
  useSessionActionStore,
} from "./sessionActionStore";

const writes = vi.hoisted(() => ({
  pin: vi.fn(),
  mute: vi.fn(),
  move: vi.fn(),
  rename: vi.fn(),
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
const ui = vi.hoisted(() => ({
  toast: vi.fn(),
  navigate: vi.fn(),
  viewing: null as string | null,
}));
vi.mock("@/components/ui/toast", () => ({ showToast: ui.toast }));
vi.mock("@/lib/routing", async (importOriginal) => ({
  ...(await importOriginal<typeof RoutingModule>()),
  useNavigate: () => ui.navigate,
}));
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

function renderHost(queryClient = new QueryClient()) {
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SessionActionHost />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useSessionActionStore.setState({ requests: [] });
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

  it("runs an action once, even on a host mounted again", () => {
    renderHost();
    act(() => requestSessionAction(SESSION, "pin"));
    cleanup();
    renderHost();
    expect(writes.pin).toHaveBeenCalledTimes(1);
  });

  describe("keeps a replaced action until its write settles", () => {
    const RUNNING: Conversation = { ...SESSION, host_id: "h1", runner_id: "r1" };

    it("still reports a Stop that fails after another action started", async () => {
      const stop = deferred();
      server.stopSession.mockReturnValue(stop.promise);
      renderHost();
      act(() => requestSessionAction(RUNNING, "stop"));
      fireEvent.click(await screen.findByTestId("stop-session-confirm"));
      act(() => requestSessionAction(SESSION, "mark-unread"));
      expect(useSessionActionStore.getState().requests).toHaveLength(2);
      await act(async () => stop.reject(new Error("runner unreachable")));
      await waitFor(() =>
        expect(ui.toast).toHaveBeenCalledWith("Couldn't stop the session: runner unreachable"),
      );
      await waitFor(() => expect(useSessionActionStore.getState().requests).toHaveLength(1));
      expect(latestSessionActionRequest()?.command).toBe("mark-unread");
    });

    it("still leaves the page after Leave succeeds, though another action started", async () => {
      const leave = deferred();
      server.revokePermission.mockReturnValue(leave.promise);
      ui.viewing = "c1";
      renderHost();
      act(() => requestSessionAction({ ...SESSION, owner: "someone-else" }, "leave"));
      fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
      await waitFor(() => expect(server.revokePermission).toHaveBeenCalledWith("c1", "viewer"));
      act(() => requestSessionAction(SESSION, "mark-unread"));
      await act(async () => leave.resolve());
      await waitFor(() => expect(ui.navigate).toHaveBeenCalledWith("/", { replace: true }));
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

    it("does nothing once a listed session has left every list", () => {
      renderHost(cachedSessions());
      act(() => requestSessionAction(SESSION, "pin", { listed: true }));
      expect(writes.pin).not.toHaveBeenCalled();
    });

    it("checks the current owner, not the one the row showed", () => {
      renderHost(cachedSessions({ ...SESSION, owner: "someone-else" }));
      act(() => requestSessionAction(SESSION, "rename", { listed: true }));
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("keeps a shared search result's owner when only its detail is cached", () => {
      const queryClient = cachedSessions();
      // The session's own fetch carries no owner; read access only.
      queryClient.setQueryData(["session", "c1"], {
        id: "c1",
        title: "Fix the parser",
        createdAt: 0,
        permissionLevel: 1,
        labels: {},
        archived: false,
      });
      renderHost(queryClient);
      act(() => requestSessionAction({ ...SESSION, owner: "someone-else" }, "rename"));
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
