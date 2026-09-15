// Secure-context-safe UUID generation.
//
// `crypto.randomUUID` is only defined in a secure context (HTTPS, or the
// `localhost` / `127.0.0.1` / `*.localhost` loopback exceptions). A self-hosted
// Omnigent served over plain `http` on an intranet host or IP is NOT a secure
// context, so calling `crypto.randomUUID()` there throws
// ("crypto.randomUUID is not a function") and breaks whatever path depends on
// it (e.g. the composer's send stable-id). Prefer the native implementation,
// fall back to an RFC-4122 v4 built from `crypto.getRandomValues` (which IS
// available in insecure contexts), and only then to `Math.random`.

/** A v4 UUID string, e.g. `"1e2f…-…-4…-…-…"`. Safe in insecure contexts. */
export function randomUUID(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = (Math.random() * 256) | 0;
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
