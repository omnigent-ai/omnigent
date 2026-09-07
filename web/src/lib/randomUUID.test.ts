import { afterEach, describe, expect, it, vi } from "vitest";
import { randomUUID } from "./randomUUID";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("randomUUID", () => {
  it("returns a well-formed v4 UUID and is unique across calls", () => {
    const ids = Array.from({ length: 1000 }, () => randomUUID());
    for (const id of ids) expect(id).toMatch(V4);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("uses the native crypto.randomUUID when present", () => {
    const native = vi.fn(() => "11111111-1111-4111-8111-111111111111");
    vi.stubGlobal("crypto", {
      randomUUID: native,
      getRandomValues: globalThis.crypto.getRandomValues,
    });
    expect(randomUUID()).toBe("11111111-1111-4111-8111-111111111111");
    expect(native).toHaveBeenCalledOnce();
  });

  it("falls back to getRandomValues when crypto.randomUUID is missing (insecure context)", () => {
    // A plain-http, non-localhost origin exposes getRandomValues but not randomUUID.
    const getRandomValues = vi.fn((arr: Uint8Array) => {
      for (let i = 0; i < arr.length; i++) arr[i] = i;
      return arr;
    });
    vi.stubGlobal("crypto", { getRandomValues });
    const id = randomUUID();
    expect(id).toMatch(V4);
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it("falls back to Math.random when crypto is entirely absent", () => {
    vi.stubGlobal("crypto", undefined);
    expect(randomUUID()).toMatch(V4);
  });
});
