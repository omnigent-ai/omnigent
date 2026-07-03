// Cmd/Ctrl+Enter accepts the newest pending accept/decline prompt; skips
// already-responded prompts and AskUserQuestion (which needs an explicit
// choice); ignores bare Enter and Alt/Shift-modified Enter.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const submitApproval = vi.fn();
let blocks: Array<Record<string, unknown>> = [];
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

  it("Cmd+Enter accepts the pending approval", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey());
    press();
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("Ctrl+Enter also accepts (Win/Linux)", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey());
    press({ ctrlKey: true });
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("accepts the most recent pending approval", () => {
    blocks = [
      { type: "elicitation", elicitationId: "old", status: "pending" },
      { type: "text" },
      { type: "elicitation", elicitationId: "new", status: "pending" },
    ];
    renderHook(() => useApproveHotkey());
    press();
    expect(submitApproval).toHaveBeenCalledWith("new", "accept");
  });

  it("ignores already-responded prompts", () => {
    blocks = [{ type: "elicitation", elicitationId: "e1", status: "responded" }];
    renderHook(() => useApproveHotkey());
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("skips AskUserQuestion (needs an explicit choice)", () => {
    blocks = [{ type: "elicitation", elicitationId: "q1", status: "pending", askUserQuestion: {} }];
    renderHook(() => useApproveHotkey());
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("ignores bare Enter and Alt/Shift-modified Enter", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey());
    press({}); // bare Enter
    press({ metaKey: true, shiftKey: true });
    press({ metaKey: true, altKey: true });
    expect(submitApproval).not.toHaveBeenCalled();
  });
});
