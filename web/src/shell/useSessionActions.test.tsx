import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";

import { useSessionActions } from "./useSessionActions";

const leave = { mutateAsync: vi.fn(), isPending: false };
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  useLeaveSession: () => leave,
  useArchiveConversation: () => idle,
  useStopAndDeleteConversation: () => idle,
  useStopSession: () => idle,
}));

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

/** One row's actions, opening its Leave confirmation once mounted. */
function Row({ onLeave }: { onLeave?: () => void }) {
  const actions = useSessionActions({
    conversation: SHARED,
    label: "Session A",
    viewerId: "viewer@example.com",
    serverInfo: "loading",
    onLeave,
    componentIdPrefix: "test.conversation",
  });
  const { setLeaveOpen } = actions;
  useEffect(() => setLeaveOpen(true), [setLeaveOpen]);
  return actions.dialogs;
}

function renderRow(onLeave?: () => void) {
  const queryClient = new QueryClient();
  const tree = (props: { onLeave?: () => void }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Row {...props} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree({ onLeave }));
  return { ...view, rerenderRow: (next?: () => void) => view.rerender(tree({ onLeave: next })) };
}

let finishLeave: () => void = () => {};
beforeEach(() => {
  leave.mutateAsync.mockReset().mockReturnValue(
    new Promise<void>((resolve) => {
      finishLeave = resolve;
    }),
  );
});
afterEach(cleanup);

describe("useSessionActions Leave", () => {
  it("leaves the view if the session is still the open one when the Leave succeeds", async () => {
    const goHome = vi.fn();
    const view = renderRow(goHome);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    // The row drops out before the request settles.
    view.unmount();
    await act(async () => finishLeave());
    expect(goHome).toHaveBeenCalledOnce();
  });

  it("stays on the session the user switched to before the Leave succeeds", async () => {
    const goHome = vi.fn();
    const view = renderRow(goHome);
    fireEvent.click(await screen.findByTestId("confirm-leave-conversation"));
    // The user opens another session, so this row stops offering a way off its view.
    view.rerenderRow(undefined);
    view.unmount();
    await act(async () => finishLeave());
    expect(goHome).not.toHaveBeenCalled();
  });
});
