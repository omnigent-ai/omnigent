import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopActionInvocation as NativeInvocation } from "@/lib/nativeBridge";
import { ActionsProvider } from "./ActionProvider";
import { DesktopActionSync } from "./DesktopActionSync";
import { resetKeybindingStoreForTesting, setUserKeybindingRule } from "./KeybindingStore";
import { useRegisterAction } from "./useRegisterAction";
import { HANDLED } from "./types";

const bridge = vi.hoisted(() => ({
  setBindings: vi.fn(),
  reportResult: vi.fn(),
  clearBindings: vi.fn(),
  unsubscribe: vi.fn(),
  invoke: null as ((invocation: NativeInvocation) => void) | null,
}));

vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => false,
  isElectronShell: () => false,
  setDesktopActionBindings: bridge.setBindings,
  clearDesktopActionBindings: bridge.clearBindings,
  reportDesktopActionResult: bridge.reportResult,
  onDesktopActionInvoked: (callback: (invocation: NativeInvocation) => void) => {
    bridge.invoke = callback;
    return bridge.unsubscribe;
  },
}));

function NewSessionHandler({ run }: { run: (source: string) => void }) {
  useRegisterAction("session.action.new", {
    scope: "global",
    acceptsKeybindings: true,
    run: ({ source }) => {
      run(source);
      return HANDLED;
    },
  });
  return null;
}

beforeEach(() => {
  localStorage.clear();
  resetKeybindingStoreForTesting();
  bridge.setBindings.mockClear();
  bridge.reportResult.mockClear();
  bridge.clearBindings.mockClear();
  bridge.unsubscribe.mockClear();
  bridge.invoke = null;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

describe("DesktopActionSync", () => {
  it("publishes the effective desktop bindings and republishes user changes", async () => {
    render(
      <ActionsProvider>
        <DesktopActionSync />
      </ActionsProvider>,
    );
    expect(bridge.setBindings).toHaveBeenCalledOnce();
    expect(bridge.setBindings.mock.calls[0]?.[0]).toMatchObject({ version: 1 });

    act(() => {
      expect(
        setUserKeybindingRule({
          id: "session.new",
          action: "session.action.new",
          sequence: "ctrl+j",
          mode: "global",
        }),
      ).toEqual({ ok: true, changed: true });
    });
    await waitFor(() => expect(bridge.setBindings).toHaveBeenCalledTimes(2));
    expect(bridge.setBindings.mock.calls[1]?.[0].bindings[0]).toEqual({
      action: "session.action.new",
      accelerator: "Ctrl+J",
    });
  });

  it("executes a native menu invocation once and reports the result", () => {
    const run = vi.fn();
    render(
      <ActionsProvider>
        <DesktopActionSync />
        <NewSessionHandler run={run} />
      </ActionsProvider>,
    );
    act(() => bridge.invoke?.({ action: "session.action.new", requestId: "request-1" }));
    expect(run).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledWith("menu");
    expect(bridge.reportResult).toHaveBeenCalledWith("request-1", true);
  });

  it("reports a synchronous handler failure as unhandled", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ActionsProvider>
        <DesktopActionSync />
        <NewSessionHandler
          run={() => {
            throw new Error("failed");
          }}
        />
      </ActionsProvider>,
    );
    act(() => bridge.invoke?.({ action: "session.action.new", requestId: "request-failed" }));
    expect(bridge.reportResult).toHaveBeenCalledWith("request-failed", false);
  });

  it("reports unhandled for unavailable or unknown actions", () => {
    render(
      <ActionsProvider>
        <DesktopActionSync />
      </ActionsProvider>,
    );
    act(() => bridge.invoke?.({ action: "file.action.find", requestId: "request-2" }));
    expect(bridge.reportResult).toHaveBeenCalledWith("request-2", false);

    act(() =>
      bridge.invoke?.({
        action: "not.an.action" as NativeInvocation["action"],
        requestId: "request-3",
      }),
    );
    expect(bridge.reportResult).toHaveBeenCalledWith("request-3", false);
  });

  it("unsubscribes from native invocations on unmount", () => {
    const view = render(
      <ActionsProvider>
        <DesktopActionSync />
      </ActionsProvider>,
    );
    view.unmount();
    expect(bridge.unsubscribe).toHaveBeenCalledOnce();
    expect(bridge.clearBindings).toHaveBeenCalledOnce();
  });
});
