import { describe, expect, it, vi } from "vitest";

import { GADGET_LIMITS, isJsonDocumentValue, parseFrameMessage } from "./gadgetProtocol";

const request = (overrides: Record<string, unknown> = {}) => ({
  v: 1,
  kind: "request",
  id: "r1_abc",
  method: "data.v1.get",
  params: { collection: "cards", id: "c1" },
  ...overrides,
});

// The frame envelope validator is a pure function of the received message.
describe("parseFrameMessage", () => {
  it("accepts a well-formed request and applies the default deadline", () => {
    expect(parseFrameMessage(request())).toEqual({
      ok: true,
      message: {
        v: 1,
        kind: "request",
        id: "r1_abc",
        method: "data.v1.get",
        params: { collection: "cards", id: "c1" },
        deadlineMs: GADGET_LIMITS.defaultDeadlineMs,
      },
    });
  });

  it.each([
    { deadlineMs: 1, expected: GADGET_LIMITS.minDeadlineMs },
    { deadlineMs: 10 * 60_000, expected: GADGET_LIMITS.maxDeadlineMs },
  ])("clamps a deadline of $deadlineMs ms to $expected ms", ({ deadlineMs, expected }) => {
    expect(parseFrameMessage(request({ deadlineMs }))).toEqual({
      ok: true,
      message: expect.objectContaining({ deadlineMs: expected }),
    });
  });

  it("accepts a cancel and rejects a cancel carrying extra fields", () => {
    expect(parseFrameMessage({ v: 1, kind: "cancel", id: "r2" })).toEqual({
      ok: true,
      message: { v: 1, kind: "cancel", id: "r2" },
    });
    expect(parseFrameMessage({ v: 1, kind: "cancel", id: "r2", method: "data.v1.get" })).toEqual({
      ok: false,
      reason: "unexpected cancel fields",
      requestId: "r2",
    });
  });

  it.each([
    {
      label: "a non-object",
      data: "hello",
      expected: { ok: false, reason: "message is not an object" },
    },
    {
      label: "another protocol version",
      data: request({ v: 2 }),
      expected: { ok: false, reason: "unsupported protocol version", requestId: "r1_abc" },
    },
    {
      label: "an unknown kind",
      data: request({ kind: "subscribe" }),
      expected: { ok: false, reason: "unknown message kind", requestId: "r1_abc" },
    },
    {
      label: "an unknown method",
      data: request({ method: "http.fetch" }),
      expected: { ok: false, reason: "unknown method", requestId: "r1_abc" },
    },
    {
      label: "an unexpected field",
      data: request({ url: "https://example.com" }),
      expected: { ok: false, reason: "unexpected request fields", requestId: "r1_abc" },
    },
    {
      label: "array params",
      data: request({ params: [] }),
      expected: { ok: false, reason: "params must be an object", requestId: "r1_abc" },
    },
    {
      label: "an invalid request id",
      data: request({ id: "not valid!" }),
      expected: { ok: false, reason: "missing or invalid request id" },
    },
  ])("rejects $label", ({ data, expected }) => {
    expect(parseFrameMessage(data)).toEqual(expected);
  });

  it("rejects a create whose document value is not supported JSON", () => {
    expect(
      parseFrameMessage(
        request({
          method: "data.v1.create",
          params: { collection: "cards", value: { due: Number.NaN }, operationId: "o1" },
        }),
      ),
    ).toEqual({ ok: false, reason: "document value is not supported JSON", requestId: "r1_abc" });
  });

  it("rejects shared references that would expand when serialized, without serializing", () => {
    // 40 levels, each holding its predecessor twice: a few dozen objects that would serialize
    // to 2^40 copies of the innermost value.
    let shared: unknown = { leaf: "x" };
    for (let level = 0; level < 40; level += 1) {
      shared = { a: shared, b: shared };
    }
    const stringify = vi.spyOn(JSON, "stringify");
    try {
      expect(
        parseFrameMessage(request({ params: { collection: "cards", extra: shared } })),
      ).toEqual({ ok: false, reason: "message exceeds the size budget", requestId: "r1_abc" });
      expect(stringify).not.toHaveBeenCalled();
    } finally {
      stringify.mockRestore();
    }
  });

  it("rejects a message nested deeper than any document could make it", () => {
    expect(
      parseFrameMessage(request({ params: { collection: "cards", extra: nestedArrays(200) } })),
    ).toEqual({ ok: false, reason: "message exceeds the size budget", requestId: "r1_abc" });
  });

  it("rejects a message larger than the size budget", () => {
    const value = "x".repeat(GADGET_LIMITS.maxMessageBytes);
    expect(parseFrameMessage(request({ params: { collection: "cards", value } }))).toEqual({
      ok: false,
      reason: "message exceeds the size budget",
      requestId: "r1_abc",
    });
  });
});

function nestedArrays(levels: number): unknown {
  let value: unknown = 0;
  for (let level = 0; level < levels; level += 1) {
    value = [value];
  }
  return value;
}

// The document-value check is a pure function of the supplied value.
describe("isJsonDocumentValue", () => {
  it("accepts nested plain JSON", () => {
    expect(
      isJsonDocumentValue({
        title: "Card",
        order: 3,
        weight: 0.5,
        owner: null,
        done: false,
        checklist: [{ text: "a", done: true }],
        meta: Object.create(null),
      }),
    ).toBe(true);
  });

  it.each([
    { label: "NaN", value: { order: Number.NaN } },
    { label: "Infinity", value: [Number.POSITIVE_INFINITY] },
    { label: "undefined", value: { owner: undefined } },
    { label: "a Date", value: { due: new Date(0) } },
    { label: "an unsafe integer", value: { id: Number.MAX_SAFE_INTEGER + 2 } },
    {
      label: "nesting deeper than the depth limit",
      value: nestedArrays(GADGET_LIMITS.maxJsonDepth + 1),
    },
  ])("rejects $label", ({ value }) => {
    expect(isJsonDocumentValue(value)).toBe(false);
  });
});
