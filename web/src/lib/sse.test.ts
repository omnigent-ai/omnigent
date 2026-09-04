// Vitest cases for `parseEvent` — the raw-SSE-JSON → typed-event mapping —
// and `withStallGuard` — the byte-level silence watchdog.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseEvent, withStallGuard } from "./sse";
import type { SessionStatusEvent, SessionSupersededEvent, TextDelta } from "./events";

describe("withStallGuard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** An upstream byte stream the test feeds, plus a cancel probe. */
  function upstream(): {
    stream: ReadableStream<Uint8Array>;
    push: (s: string) => void;
    cancelled: () => boolean;
  } {
    let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        ctrl = c;
      },
      cancel() {
        cancelled = true;
      },
    });
    return {
      stream,
      push: (s) => ctrl!.enqueue(new TextEncoder().encode(s)),
      cancelled: () => cancelled,
    };
  }

  it("passes chunks through while bytes flow, then trips on silence", async () => {
    const up = upstream();
    let stalled = 0;
    const reader = withStallGuard(up.stream, {
      stallMs: 1_000,
      onStall: () => (stalled += 1),
    }).getReader();

    // Bytes inside the window pass through and re-arm the timer.
    up.push("a");
    expect(new TextDecoder().decode((await reader.read()).value)).toBe("a");
    await vi.advanceTimersByTimeAsync(900);
    up.push("b");
    expect(new TextDecoder().decode((await reader.read()).value)).toBe("b");
    expect(stalled).toBe(0);

    // Silence past the window: the upstream is cancelled and the guarded
    // stream ends cleanly (done, not an error) — the same shape as a
    // transport drop, which consumers answer with a reconnect.
    const pending = reader.read();
    await vi.advanceTimersByTimeAsync(1_100);
    expect((await pending).done).toBe(true);
    expect(stalled).toBe(1);
    expect(up.cancelled()).toBe(true);
  });

  it("propagates a consumer cancel upstream without tripping", async () => {
    const up = upstream();
    let stalled = 0;
    const guarded = withStallGuard(up.stream, { stallMs: 1_000, onStall: () => (stalled += 1) });

    await guarded.cancel();
    expect(up.cancelled()).toBe(true);

    // The armed timer was cleared: silence after cancel is not a stall.
    await vi.advanceTimersByTimeAsync(5_000);
    expect(stalled).toBe(0);
  });
});

describe("parseEvent — response.output_text.delta", () => {
  it("parses a plain delta with no streaming identifiers", () => {
    // Ordinary in-process task streaming: only `delta` is present, and
    // the native-scoping fields stay undefined so downstream treats it
    // as response-scoped (not message-scoped) text.
    const ev = parseEvent("response.output_text.delta", { delta: "Hi" });
    expect(ev).toEqual({
      type: "text_delta",
      delta: "Hi",
      messageId: undefined,
      index: undefined,
      final: undefined,
    } satisfies TextDelta);
  });

  it("threads message_id / index / final for claude-native streaming", () => {
    const ev = parseEvent("response.output_text.delta", {
      delta: "Hel",
      message_id: "m1",
      index: 0,
      final: false,
    });
    // All three native fields surface so the store can scope, order, and
    // finalize the in-flight buffer. index 0 and final false must NOT be
    // coerced to undefined (they're meaningful falsy values).
    expect(ev).toEqual({
      type: "text_delta",
      delta: "Hel",
      messageId: "m1",
      index: 0,
      final: false,
    } satisfies TextDelta);
  });

  it("ignores wrong-typed streaming identifiers rather than poisoning the buffer", () => {
    const ev = parseEvent("response.output_text.delta", {
      delta: "x",
      message_id: 7,
      index: "0",
      final: "yes",
    });
    // A malformed field is dropped (left undefined), so the delta still
    // renders as plain text instead of keying a buffer on garbage.
    expect(ev).toEqual({
      type: "text_delta",
      delta: "x",
      messageId: undefined,
      index: undefined,
      final: undefined,
    } satisfies TextDelta);
  });

  it("returns null when delta is not a string", () => {
    expect(parseEvent("response.output_text.delta", { delta: { text: "bad" } })).toBeNull();
  });
});

describe("parseEvent — session.superseded", () => {
  it("parses the carrier + redirect target", () => {
    const ev = parseEvent("session.superseded", {
      conversation_id: "conv_old",
      target_conversation_id: "conv_new",
      reason: "clear",
    });
    expect(ev).toEqual({
      type: "session_superseded",
      conversationId: "conv_old",
      targetConversationId: "conv_new",
      reason: "clear",
    } satisfies SessionSupersededEvent);
  });

  it("returns null when the target conversation id is missing", () => {
    expect(parseEvent("session.superseded", { conversation_id: "conv_old" })).toBeNull();
  });

  it("returns null when the carrier conversation id is missing", () => {
    expect(parseEvent("session.superseded", { target_conversation_id: "conv_new" })).toBeNull();
  });
});

describe("parseEvent — session.status (blocked_on)", () => {
  function blockedOn(data: Record<string, unknown>): string | undefined {
    const ev = parseEvent("session.status", {
      conversation_id: "conv_a",
      status: "running",
      ...data,
    });
    return (ev as SessionStatusEvent | null)?.blockedOn;
  }

  it("threads the reason so the indicator can name what the agent is parked on", () => {
    expect(blockedOn({ blocked_on: "permission prompt" })).toBe("permission prompt");
  });

  it("leaves it undefined when absent (the session is not parked)", () => {
    expect(blockedOn({})).toBeUndefined();
  });

  it("ignores an empty or non-string reason rather than rendering a blank one", () => {
    expect(blockedOn({ blocked_on: "" })).toBeUndefined();
    expect(blockedOn({ blocked_on: 7 })).toBeUndefined();
  });
});

describe("parseEvent — session.status (background_task_count)", () => {
  function bgCount(data: Record<string, unknown>): number | undefined {
    const ev = parseEvent("session.status", { conversation_id: "conv_a", status: "idle", ...data });
    return (ev as SessionStatusEvent | null)?.backgroundTaskCount;
  }

  it("threads a positive count so the indicator can show 'N background tasks still running'", () => {
    expect(bgCount({ background_task_count: 3 })).toBe(3);
  });

  it("keeps an explicit 0 (authoritative clear) rather than collapsing it to undefined", () => {
    // A Stop hook reporting zero remaining shells must reach the store as `0`
    // so the sticky tally clears; `undefined` would leave a finished shell's
    // indicator stuck on screen.
    expect(bgCount({ background_task_count: 0 })).toBe(0);
  });

  it("leaves the count undefined when the field is absent (no information)", () => {
    // The PTY-activity `idle` carries no count; absent must stay sticky-safe
    // (undefined), distinct from an authoritative 0.
    expect(bgCount({})).toBeUndefined();
  });

  it("ignores a non-numeric or negative count", () => {
    expect(bgCount({ background_task_count: "2" })).toBeUndefined();
    expect(bgCount({ background_task_count: -1 })).toBeUndefined();
  });
});

describe("parseEvent — session.status (background_tasks detail)", () => {
  function bgTasks(data: Record<string, unknown>) {
    const ev = parseEvent("session.status", { conversation_id: "conv_a", status: "idle", ...data });
    return (ev as SessionStatusEvent | null)?.backgroundTasks;
  }

  it("threads the per-shell detail so the UI can list the shells", () => {
    expect(
      bgTasks({
        background_task_count: 2,
        background_tasks: [
          {
            id: "a",
            type: "shell",
            status: "running",
            description: "Wait for CI",
            command: "sleep 120",
          },
          { description: "Build check" },
        ],
      }),
    ).toEqual([
      {
        id: "a",
        type: "shell",
        status: "running",
        description: "Wait for CI",
        command: "sleep 120",
      },
      { description: "Build check" },
    ]);
  });

  it("drops non-object entries and entries with no usable string field", () => {
    expect(
      bgTasks({ background_tasks: ["garbage", 5, null, { description: "keep me" }, { id: 42 }] }),
    ).toEqual([{ description: "keep me" }]);
  });

  it("leaves detail undefined when absent, not an array, or empty after filtering", () => {
    expect(bgTasks({})).toBeUndefined();
    expect(bgTasks({ background_tasks: "nope" })).toBeUndefined();
    expect(bgTasks({ background_tasks: [] })).toBeUndefined();
    expect(bgTasks({ background_tasks: ["junk", 1] })).toBeUndefined();
  });
});

describe("parseEvent — session.mcp_startup", () => {
  it("parses a per-server startup map for the MCP startup band", () => {
    const ev = parseEvent("session.mcp_startup", {
      conversation_id: "conv_a",
      servers: {
        safe: { status: "failed", error: "handshake failed" },
        "storage-console": { status: "starting", error: null },
      },
    });
    expect(ev).toEqual({
      type: "session_mcp_startup",
      conversationId: "conv_a",
      servers: {
        safe: { status: "failed", error: "handshake failed" },
        "storage-console": { status: "starting", error: null },
      },
    });
  });

  it("skips entries with unknown statuses instead of dropping the frame", () => {
    // A partial map still updates the band; a bogus status must not reach
    // the store where it would render an unknown state.
    const ev = parseEvent("session.mcp_startup", {
      conversation_id: "conv_a",
      servers: {
        ok: { status: "ready" },
        bad: { status: "exploded" },
      },
    });
    expect(ev).toEqual({
      type: "session_mcp_startup",
      conversationId: "conv_a",
      servers: { ok: { status: "ready", error: null } },
    });
  });

  it("rejects frames without a conversation id or servers map", () => {
    expect(parseEvent("session.mcp_startup", { servers: {} })).toBeNull();
    expect(
      parseEvent("session.mcp_startup", { conversation_id: "conv_a", servers: "nope" }),
    ).toBeNull();
  });
});

describe("parseEvent — response.output_item.done error level", () => {
  it("lifts level: info onto the error event and omits it otherwise", () => {
    const item = {
      id: "err_1",
      response_id: "resp_1",
      type: "error",
      source: "harness",
      code: "codex_thread_reset",
      message: "Codex started a fresh thread.",
    };
    const info = parseEvent("response.output_item.done", { item: { ...item, level: "info" } });
    expect(info).toMatchObject({
      type: "error",
      error: { code: "codex_thread_reset", level: "info" },
    });
    const plain = parseEvent("response.output_item.done", { item });
    const plainError = plain?.type === "error" ? plain.error : null;
    expect(plainError).not.toBeNull();
    expect(plainError).not.toHaveProperty("level");
  });
});
