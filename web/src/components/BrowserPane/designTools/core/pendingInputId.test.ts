import { afterEach, describe, expect, it, vi } from "vitest";
import { nativePendingId, sha1 } from "./pendingInputId";

const hex = (bytes: Uint8Array) =>
  [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("nativePendingId", () => {
  it("names a request's pending input as the server does", async () => {
    // make_pending_id("conv_outbox", ...) in omnigent/runtime/pending_inputs.py
    expect(await nativePendingId("conv_outbox", "0123456789abcdef0123456789abcdef")).toBe(
      "pending_5ba89dae29a15245929ebf6e5221a55d",
    );
  });

  it("names it the same without crypto.subtle (an insecure context)", async () => {
    vi.stubGlobal("crypto", {});
    expect(await nativePendingId("conv_outbox", "0123456789abcdef0123456789abcdef")).toBe(
      "pending_5ba89dae29a15245929ebf6e5221a55d",
    );
  });
});

describe("sha1", () => {
  it("matches the standard digests, across block boundaries", () => {
    const encode = (text: string) => new TextEncoder().encode(text);
    expect(hex(sha1(encode("")))).toBe("da39a3ee5e6b4b0d3255bfef95601890afd80709");
    expect(hex(sha1(encode("abc")))).toBe("a9993e364706816aba3e25717850c26c9cd0d89d");
    expect(hex(sha1(encode("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")))).toBe(
      "84983e441c3bd26ebaae4aa1f95129e5e54670f1",
    );
    expect(hex(sha1(encode("a".repeat(1000))))).toBe("291e9a6c66994949b57ba5e650361e98fc36b1ba");
  });
});
