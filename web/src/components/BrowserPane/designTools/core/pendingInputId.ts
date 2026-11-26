// The id a native terminal session's server gives a posted message while it waits for the
// agent (`make_pending_id` in omnigent/runtime/pending_inputs.py): the outbox hears that the
// message arrived by this id, which the session's receipt names.

// uuid5's namespace for URLs (RFC 4122).
const URL_NAMESPACE = "6ba7b8119dad11d180b400c04fd430c8";

/** SHA-1 of `input` (FIPS 180-4), for pages without `crypto.subtle` (insecure contexts). */
export function sha1(input: Uint8Array): Uint8Array {
  const length = input.length;
  const padded = new Uint8Array((((length + 8) >> 6) + 1) * 64);
  padded.set(input);
  padded[length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(length / 0x20000000));
  view.setUint32(padded.length - 4, (length * 8) >>> 0);
  const h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0];
  const w = new Uint32Array(80);
  for (let block = 0; block < padded.length; block += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(block + i * 4);
    for (let i = 16; i < 80; i++) {
      const x = w[i - 3]! ^ w[i - 8]! ^ w[i - 14]! ^ w[i - 16]!;
      w[i] = (x << 1) | (x >>> 31);
    }
    let [a, b, c, d, e] = h as [number, number, number, number, number];
    for (let i = 0; i < 80; i++) {
      const [f, k] =
        i < 20
          ? [(b & c) | (~b & d), 0x5a827999]
          : i < 40
            ? [b ^ c ^ d, 0x6ed9eba1]
            : i < 60
              ? [(b & c) | (b & d) | (c & d), 0x8f1bbcdc]
              : [b ^ c ^ d, 0xca62c1d6];
      const next = (((a << 5) | (a >>> 27)) + f + e + k + w[i]!) >>> 0;
      e = d;
      d = c;
      c = (b << 30) | (b >>> 2);
      b = a;
      a = next;
    }
    h[0] = (h[0]! + a) >>> 0;
    h[1] = (h[1]! + b) >>> 0;
    h[2] = (h[2]! + c) >>> 0;
    h[3] = (h[3]! + d) >>> 0;
    h[4] = (h[4]! + e) >>> 0;
  }
  const out = new Uint8Array(20);
  const outView = new DataView(out.buffer);
  h.forEach((word, i) => outView.setUint32(i * 4, word));
  return out;
}

/** The server's pending-input id for a stable id. */
export async function nativePendingId(sessionId: string, stableId: string): Promise<string> {
  const namespace = Uint8Array.from(URL_NAMESPACE.match(/../g)!, (byte) => parseInt(byte, 16));
  const name = new TextEncoder().encode(`omnigent:pending-input:${sessionId}:${stableId}`);
  const input = new Uint8Array(namespace.length + name.length);
  input.set(namespace);
  input.set(name, namespace.length);
  const digest = globalThis.crypto?.subtle
    ? new Uint8Array(await crypto.subtle.digest("SHA-1", input))
    : sha1(input);
  const hash = digest.slice(0, 16);
  hash[6] = (hash[6]! & 0x0f) | 0x50;
  hash[8] = (hash[8]! & 0x3f) | 0x80;
  return `pending_${[...hash].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
