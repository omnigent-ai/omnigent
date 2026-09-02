import { useEffect, useRef } from "react";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ActionScopeProvider,
  ActionsProvider,
  HANDLED,
  KeybindingDispatcher,
  NOT_HANDLED,
  useActionScopeRegistration,
  useRegisterAction,
} from "@/actions";
import { TerminalSession } from "./TerminalSession";

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  binaryType = "blob";
  sent: unknown[] = [];
  private listeners: Record<string, ((event: unknown) => void)[]> = {};

  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    (this.listeners[type] ??= []).push(listener);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close() {}

  open() {
    this.readyState = FakeWebSocket.OPEN;
    for (const listener of this.listeners.open ?? []) listener({});
  }
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

function payloads(socket: FakeWebSocket): number[][] {
  return socket.sent
    .filter((message) => ArrayBuffer.isView(message))
    .map((message) => Array.from(message as unknown as Uint8Array));
}

function TerminalHarness() {
  const mountRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<TerminalSession | null>(null);
  const scope = useActionScopeRegistration({ mode: "terminal" });

  useRegisterAction("terminal.action.sendSequence", {
    scope: scope.id,
    acceptsKeybindings: true,
    isEnabled: () => sessionRef.current !== null,
    run: ({ args }) => {
      if (!sessionRef.current) return NOT_HANDLED;
      sessionRef.current.sendInput(args.data);
      return HANDLED;
    },
  });

  useEffect(() => {
    if (!mountRef.current) return;
    const session = new TerminalSession(mountRef.current, "ws://localhost/attach", () => {});
    sessionRef.current = session;
    FakeWebSocket.instances.at(-1)!.open();
    return () => {
      sessionRef.current = null;
      session.dispose();
    };
  }, []);

  return (
    <div {...scope.rootProps}>
      <ActionScopeProvider scope={scope}>
        <div ref={mountRef} />
      </ActionScopeProvider>
    </div>
  );
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("terminal keybinding integration", () => {
  it("sends exactly one CSI-u payload and no bare Enter through real xterm", async () => {
    const { container } = render(
      <ActionsProvider>
        <KeybindingDispatcher />
        <TerminalHarness />
      </ActionsProvider>,
    );
    await waitFor(() => expect(container.querySelector(".xterm-helper-textarea")).not.toBeNull());
    const textarea = container.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement;
    const socket = FakeWebSocket.instances[0]!;
    socket.sent = [];
    textarea.focus();

    fireEvent.keyDown(textarea, { key: "Enter", keyCode: 13, shiftKey: true });
    expect(payloads(socket)).toEqual([[27, 91, 49, 51, 59, 50, 117]]);

    socket.sent = [];
    fireEvent.keyDown(textarea, { key: "Enter", keyCode: 13 });
    expect(payloads(socket)).toEqual([[13]]);
  });
});
