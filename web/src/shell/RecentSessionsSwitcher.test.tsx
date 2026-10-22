import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { RecentSessionsSwitcher } from "./RecentSessionsSwitcher";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  navigate.mockReset();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

function conversation(
  id: string,
  updatedAt: number,
  options: { archived?: boolean; provisional?: boolean } = {},
): Conversation {
  return {
    id,
    object: "conversation",
    title: `Session ${id}`,
    created_at: updatedAt,
    updated_at: updatedAt,
    labels: {},
    archived: options.archived ?? false,
    provisional: options.provisional,
    agent_name: "Claude",
  } as Conversation;
}

function pressTab(options: { shift?: boolean } = {}) {
  fireEvent.keyDown(window, {
    key: "Tab",
    code: "Tab",
    ctrlKey: true,
    shiftKey: options.shift ?? false,
  });
}

describe("RecentSessionsSwitcher", () => {
  it("shows only the five most recently active real sessions", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[
          conversation("one", 1),
          conversation("seven", 7, { archived: true }),
          conversation("three", 3),
          conversation("six", 6, { provisional: true }),
          conversation("five", 5),
          conversation("two", 2),
          conversation("four", 4),
        ]}
        activeSessionId={null}
        enabled
      />,
    );

    pressTab();

    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Session fiveClaude",
      "Session fourClaude",
      "Session threeClaude",
      "Session twoClaude",
      "Session oneClaude",
    ]);
  });

  it("cycles from the active session, reverses with Shift, and switches on Control release", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[
          conversation("three", 3),
          conversation("one", 1),
          conversation("four", 4),
          conversation("two", 2),
        ]}
        activeSessionId="four"
        enabled
      />,
    );

    pressTab();
    expect(screen.getByText("Session three").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    pressTab();
    expect(screen.getByText("Session two").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    pressTab({ shift: true });
    expect(screen.getByText("Session three").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });
    expect(navigate).toHaveBeenCalledWith("/c/three");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels with Escape without switching on the later Control release", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    pressTab();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape", code: "Escape", ctrlKey: true });
    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });

    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels without switching when the window loses focus", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    pressTab();
    fireEvent.blur(window);
    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });

    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("switches from keyboard input forwarded by an embedded Browser page", () => {
    let forwardInput: ((input: Record<string, unknown>) => void) | undefined;
    const unsubscribe = vi.fn();
    const setSupported = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: (callback: (input: Record<string, unknown>) => void) => {
        forwardInput = callback;
        return unsubscribe;
      },
      browserSetRecentSessionSwitchSupported: setSupported,
    };
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );
    expect(setSupported).toHaveBeenCalledWith(true);

    act(() => {
      forwardInput?.({
        type: "keydown",
        key: "Tab",
        code: "Tab",
        ctrlKey: true,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    act(() => {
      forwardInput?.({
        type: "keyup",
        key: "Control",
        code: "ControlLeft",
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });

    expect(navigate).toHaveBeenCalledWith("/c/one");
    cleanup();
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(setSupported).toHaveBeenLastCalledWith(false);
  });

  it("releases native interception when a forwarded gesture has no sessions", () => {
    let forwardInput: ((input: Record<string, unknown>) => void) | undefined;
    const cancelRecentSessionSwitch = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: (callback: (input: Record<string, unknown>) => void) => {
        forwardInput = callback;
        return vi.fn();
      },
      browserCancelRecentSessionSwitch: cancelRecentSessionSwitch,
    };
    render(<RecentSessionsSwitcher conversations={[]} activeSessionId={null} enabled />);

    act(() => {
      forwardInput?.({
        type: "keydown",
        key: "Tab",
        code: "Tab",
        ctrlKey: true,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(cancelRecentSessionSwitch).toHaveBeenCalledOnce();
  });

  it("leaves Ctrl+Tab untouched outside Electron", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("one", 1)]}
        activeSessionId={null}
        enabled={false}
      />,
    );
    const keyboardEvent = new KeyboardEvent("keydown", {
      key: "Tab",
      code: "Tab",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });

    window.dispatchEvent(keyboardEvent);

    expect(keyboardEvent.defaultPrevented).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
