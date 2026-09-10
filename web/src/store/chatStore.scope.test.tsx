import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { conversationRegistry } from "./conversationRegistry";
import { ChatStoreScopeProvider, bindConversationForTest, useChatStore } from "./chatStore";

function StatusProbe({ label }: { label: string }) {
  const status = useChatStore((state) => state.status);
  return <div data-testid={label}>{status}</div>;
}

function TodosProbe({ label, seen }: { label: string; seen: unknown[] }) {
  const todos = useChatStore((state) => state.todos);
  seen.push(todos);
  return <div data-testid={label}>{todos.length}</div>;
}

describe("ChatStoreScopeProvider", () => {
  beforeEach(() => {
    conversationRegistry.clear();
    bindConversationForTest(null);
  });

  it("selects and updates each conversation entry independently", () => {
    bindConversationForTest("session-a", { status: "streaming" });
    const sessionB = conversationRegistry.acquire("session-b");
    sessionB.setState({ status: "idle" });

    render(
      <>
        <ChatStoreScopeProvider conversationId="session-a">
          <StatusProbe label="session-a" />
        </ChatStoreScopeProvider>
        <ChatStoreScopeProvider conversationId="session-b">
          <StatusProbe label="session-b" />
        </ChatStoreScopeProvider>
      </>,
    );

    expect(screen.getByTestId("session-a")).toHaveTextContent("streaming");
    expect(screen.getByTestId("session-b")).toHaveTextContent("idle");

    act(() => sessionB.setState({ status: "streaming" }));

    expect(screen.getByTestId("session-a")).toHaveTextContent("streaming");
    expect(screen.getByTestId("session-b")).toHaveTextContent("streaming");
    expect(useChatStore.getState().conversationId).toBe("session-a");
  });

  it("shows initial conversation state when the scoped entry is not live", () => {
    // A pane whose conversation has no registry entry (e.g. a restored split
    // layout before the background bind lands) must never paint the root
    // store — that is a DIFFERENT conversation's transcript.
    bindConversationForTest("session-a", { status: "streaming" });
    render(
      <ChatStoreScopeProvider conversationId="session-missing">
        <StatusProbe label="session-missing" />
      </ChatStoreScopeProvider>,
    );
    expect(screen.getByTestId("session-missing")).toHaveTextContent("idle");
  });

  it("returns stable snapshots while the scoped entry is missing", () => {
    // useSyncExternalStore tears when getSnapshot returns a fresh reference
    // per call: every field of the fallback state must keep its identity
    // across renders, or array/object selectors loop "Maximum update depth".
    bindConversationForTest("session-a");
    const seen: unknown[] = [];
    render(
      <ChatStoreScopeProvider conversationId="session-missing">
        <TodosProbe label="todos" seen={seen} />
      </ChatStoreScopeProvider>,
    );
    expect(screen.getByTestId("todos")).toHaveTextContent("0");
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((todos) => todos === seen[0])).toBe(true);
  });
});
