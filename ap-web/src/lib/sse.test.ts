// Vitest cases for `parseEvent` — the raw-SSE-JSON → typed-event mapping.

import { describe, expect, it } from "vitest";
import { parseEvent } from "./sse";
import type { SessionModeEvent, TextDelta } from "./events";

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

describe("parseEvent — session.mode", () => {
  it("parses a valid session.mode event", () => {
    const ev = parseEvent("session.mode", {
      conversation_id: "conv_abc",
      mode: "auto",
    });
    expect(ev).toEqual({
      type: "session_mode",
      conversationId: "conv_abc",
      mode: "auto",
    } satisfies SessionModeEvent);
  });

  it("returns null when conversation_id is missing", () => {
    expect(parseEvent("session.mode", { mode: "auto" })).toBeNull();
  });

  it("returns null when mode is missing", () => {
    expect(parseEvent("session.mode", { conversation_id: "conv_abc" })).toBeNull();
  });
});
