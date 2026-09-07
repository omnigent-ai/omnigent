// Cmd/Ctrl+Enter accepts the newest pending accept/decline prompt; skips
// already-responded prompts and AskUserQuestion (which needs an explicit
// choice); platform-aware (only ⌘ on macOS, only Ctrl on Win/Linux); ignores
// bare Enter and Alt/Shift-modified Enter.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const submitApproval = vi.fn();
let blocks: Record<string, unknown>[] = [];
vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: () => ({ blocks, submitApproval }) },
}));

import { useApproveHotkey } from "./useApproveHotkey";

/** Dispatch a keydown that reaches window from body (default: Cmd+Enter). */
function press(
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  key = "Enter",
): void {
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods }),
  );
}

beforeEach(() => {
  submitApproval.mockClear();
  blocks = [];
});
afterEach(() => {
  blocks = [];
});

describe("useApproveHotkey", () => {
  const pending = { type: "elicitation", elicitationId: "e1", status: "pending" };

  const FIELD_SCHEMA = {
    type: "object",
    properties: { branch: { type: "string" } },
    required: ["branch"],
  };

  it("does not accept a prompt that asks for fields", () => {
    // The card disables Submit until the required fields are answered. A
    // keystroke that accepts anyway walks around that gate and sends the
    // server none of what it asked for.
    blocks = [{ ...pending, requestedSchema: FIELD_SCHEMA }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("does not reach past a form to accept an older binary prompt", () => {
    // The newest prompt is the one on screen. Accepting an older one while
    // someone fills in a form approves something they cannot see.
    blocks = [
      { type: "elicitation", elicitationId: "old", status: "pending" },
      { ...pending, elicitationId: "form", requestedSchema: FIELD_SCHEMA },
    ];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("still accepts a bare consent prompt that names no fields", () => {
    blocks = [{ ...pending, requestedSchema: { type: "object" } }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("Cmd+Enter accepts the pending approval (macOS)", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("Ctrl+Enter accepts on Windows/Linux", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    press({ ctrlKey: true });
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("ignores Ctrl+Enter on macOS (only ⌘↵ fires there)", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press({ ctrlKey: true });
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("accepts the most recent pending approval", () => {
    blocks = [
      { type: "elicitation", elicitationId: "old", status: "pending" },
      { type: "text" },
      { type: "elicitation", elicitationId: "new", status: "pending" },
    ];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("new", "accept");
  });

  it("ignores already-responded prompts", () => {
    blocks = [{ type: "elicitation", elicitationId: "e1", status: "responded" }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("skips AskUserQuestion (needs an explicit choice)", () => {
    blocks = [{ type: "elicitation", elicitationId: "q1", status: "pending", askUserQuestion: {} }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("ignores bare Enter and Alt/Shift-modified Enter", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press({}); // bare Enter
    press({ metaKey: true, shiftKey: true });
    press({ metaKey: true, altKey: true });
    expect(submitApproval).not.toHaveBeenCalled();
  });
});
