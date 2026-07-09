/** Normalize a user-typed address into a navigable URL.
 *
 *  Scheme selection mirrors Chrome's omnibox behavior for intranet hosts: a
 *  DOTLESS hostname (go/, wiki/, jira/FOO-1) gets `http://` — these are
 *  intranet shortname redirectors that (a) can't obtain a public TLS cert for
 *  the bare name, so https on the bare host dies on a name mismatch, and
 *  (b) immediately 302 to a real https destination anyway. Everything with a
 *  dot keeps the https-first default.
 */
export function normalizeTypedUrl(raw: string): string {
  const trimmed = raw.trim();
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  // Hostname = everything before the first / ? or #. Strip an optional
  // port before the dot test so `localhost:3000/x` counts as dotless.
  const host = trimmed.split(/[/?#]/, 1)[0].replace(/:\d+$/, "");
  const dotless = host.length > 0 && !host.includes(".");
  return (dotless ? "http://" : "https://") + trimmed;
}
