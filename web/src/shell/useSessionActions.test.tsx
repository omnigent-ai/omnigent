import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";

import { usePublishOpenSession } from "./openSession";
import { useSessionActions } from "./useSessionActions";

const leave = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
const stop = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  useLeaveSession: () => leave,
  useArchiveConversation: () => idle,
  useStopAndDeleteConversation: () => idle,
  useStopSession: () => stop,
}));

const toasts = vi.hoisted(() => [] as string[]);
vi.mock("@/components/ui/toast", () => ({ showToast: (message: string) => toasts.push(message) }));

// The open session's root is the session itself here; sub-agents aren't part of these cases.
vi.mock("@/hooks/useSession", () => ({ useActiveRootSessionId: (id: string | null) => id }));

const SHARED: Conversation = {
  id: "conv_a",
  object: "conversation",
  title: "Session A",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
  owner: "owner@example.com",
};

/** One row's actions, opening its Leave confirmation once mounted, reporting as ⌘K's host does. */
function Row({
  onLeave,
  reportsAfterUnmount = true,
}: {
  onLeave?: () => void;
  reportsAfterUnmount?: boolean;
}) {
  const actions = useSessionActions({
    conversation: SHARED,
    label: "Session A",
    viewerId: "viewer@example.com",
    serverInfo: "loading",
    onLeave,
    componentIdPrefix: "test.conversation",
    reportsAfterUnmount,
  });
  const { setLeaveOpen } = actions;
  useEffect(() => setLeaveOpen(true), [setLeaveOpen]);
  return actions.dialogs;
}

let goTo: (path: string) => void = () => {};
let dropRow: () => void = () => {};
/** The row, until the test drops it from the list as a successful Leave does. */
function ListedRow({ onLeave }: { onLeave: () => void }) {
  const [listed, setListed] = useState(true);
  dropRow = () => setListed(false);
  return listed ? <Row onLeave={onLeave} /> : null;
}

/** Publishes the routed session as the app shell does, and lets a test navigate. */
function Shell() {
  goTo = useNavigate();
  const routed = /^\/c\/([^/]+)/.exec(useLocation().pathname)?.[1] ?? null;
  usePublishOpenSession(routed);
  return null;
}

/** Renders session A's row while `/c/conv_a` is open, as the sidebar's active row, at `path`. */
function renderAt(path: string, onLeave: () => void) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Shell />
        <Routes>
          <Route path="/c/conv_a" element={<ListedRow onLeave={onLeave} />} />
          <Route path="/" element={<ListedRow onLeave={onLeave} />} />
          <Route path="*" element={null} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let finishLeave: () => void = () => {};
let failLeave: (error: Error) => void = () => {};
beforeEach(() => {
  toasts.length = 0;
  leave.mutate.mockReset();
  leave.mutateAsync.mockReset().mockReturnValue(
    new Promise<void>((resolve, reject) => {
      finishLeave = resolve;
      failLeave = reject;
    }),
  );
  stop.mutate.mockReset();
  stop.mutateAsync.mockReset();
});
afterEach(cleanup);

describe("useSessionActions Leave, reporting after its caller unmounts", () => {
  it("leaves the view if the session is still the open one when the Leave succeeds", async () => {
    const goHome = vi.fn();
    renderAt("/c/conv_a", goHome);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    // The row drops out of the list before the request settles; the session stays open.
    act(() => dropRow());
    await act(async () => finishLeave());
    expect(goHome).toHaveBeenCalledOnce();
    expect(goHome).toHaveBeenCalledWith("leave");
  });

  it("stays on the session the user switched to before the Leave succeeds", async () => {
    const goHome = vi.fn();
    renderAt("/c/conv_a", goHome);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    // Opening B unmounts A's row in the same render, with its active callback still held.
    act(() => goTo("/c/conv_b"));
    expect(screen.queryByTestId("confirm-leave-conversation")).toBeNull();
    await act(async () => finishLeave());
    expect(goHome).not.toHaveBeenCalled();
  });

  it("says the Leave failed once its row has gone, and stays on the session", async () => {
    const goHome = vi.fn();
    renderAt("/c/conv_a", goHome);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    act(() => dropRow());
    await act(async () => failLeave(new Error("offline")));
    expect(toasts).toEqual(["Couldn't leave the session: offline"]);
    expect(goHome).not.toHaveBeenCalled();
  });

  it("runs the caller's callback for a Leave started away from the session's view", async () => {
    const closeQuickView = vi.fn();
    renderAt("/", closeQuickView);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    await act(async () => finishLeave());
    expect(closeQuickView).toHaveBeenCalledOnce();
  });
});

describe("useSessionActions Leave, by default", () => {
  it("reports through the row's own mutate callbacks, as the sidebar always has", async () => {
    const goHome = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/c/conv_a"]}>
          <Row onLeave={goHome} reportsAfterUnmount={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    expect(leave.mutateAsync).not.toHaveBeenCalled();
    expect(leave.mutate).toHaveBeenCalledWith(
      { id: "conv_a", viewerId: "viewer@example.com" },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
    act(() => leave.mutate.mock.calls[0]![1].onSuccess());
    expect(goHome).toHaveBeenCalledOnce();
    expect(goHome).toHaveBeenCalledWith("leave");
  });
});

/** One row's Stop confirmation, reporting as ⌘K's host does. */
function StopRow() {
  const actions = useSessionActions({
    conversation: SHARED,
    label: "Session A",
    viewerId: "viewer@example.com",
    serverInfo: "loading",
    componentIdPrefix: "test.conversation",
    reportsAfterUnmount: true,
  });
  const { setStopOpen } = actions;
  useEffect(() => setStopOpen(true), [setStopOpen]);
  return actions.dialogs;
}

describe("useSessionActions Stop, reporting after its caller unmounts", () => {
  it("says the Stop failed once its row has gone", async () => {
    let failStop: (error: Error) => void = () => {};
    stop.mutateAsync.mockReturnValue(
      new Promise<void>((_resolve, reject) => {
        failStop = reject;
      }),
    );
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <StopRow />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Stop session" }));
    expect(stop.mutateAsync).toHaveBeenCalledWith("conv_a");
    view.unmount();
    await act(async () => failStop(new Error("host offline")));
    expect(toasts).toEqual(["Couldn't stop the session: host offline"]);
  });
});
