import { describe, expect, it } from "vitest";

import type { StateChainState } from "./pullRequestStateChain";
import {
  STATE_CHANGE_BUSY_REASON,
  STATE_CHANGE_UNKNOWN_REASON,
  stateChangeHeldReason,
  stateChangeOperationFor,
} from "./pullRequestStateChanges";

const idle: StateChainState = { running: null, notice: null };
const boundTo = {
  authGeneration: 1,
  actor: { login: "mac-octo", generation: 1 },
  reader: null,
  read: {} as never,
  scope: {} as never,
  host: { conversationId: "conv_1", key: "host-route" },
};

describe("stateChangeOperationFor", () => {
  it("closes an open PR, drafts included, reopens a closed one, and offers nothing else", () => {
    expect(stateChangeOperationFor("OPEN")).toBe("pull_request_close");
    expect(stateChangeOperationFor("open")).toBe("pull_request_close");
    expect(stateChangeOperationFor("CLOSED")).toBe("pull_request_reopen");
    expect(stateChangeOperationFor("MERGED")).toBeNull();
    expect(stateChangeOperationFor(null)).toBeNull();
    expect(stateChangeOperationFor(undefined)).toBeNull();
  });
});

describe("stateChangeHeldReason", () => {
  it("is free when nothing runs or holds", () => {
    expect(stateChangeHeldReason(idle, false)).toBeNull();
  });

  it("waits while a close, reopen, or a draft or auto-merge change runs", () => {
    expect(stateChangeHeldReason({ running: "pull_request_close", notice: null }, false)).toBe(
      STATE_CHANGE_BUSY_REASON,
    );
    expect(stateChangeHeldReason(idle, true)).toBe(STATE_CHANGE_BUSY_REASON);
  });

  it("asks for Check Again after an unknown outcome, but not after a refusal", () => {
    const unknown: StateChainState = {
      running: null,
      notice: {
        operation: "pull_request_close",
        attempt: "attempt-test",
        outcome: { kind: "unknown", commentPosted: false },
        boundTo,
      },
    };
    expect(stateChangeHeldReason(unknown, false)).toBe(STATE_CHANGE_UNKNOWN_REASON);
    const refused: StateChainState = {
      running: null,
      notice: {
        operation: "pull_request_close",
        attempt: "attempt-test",
        outcome: { kind: "refused", message: "No", commentPosted: false },
        boundTo,
      },
    };
    expect(stateChangeHeldReason(refused, false)).toBeNull();
  });
});
