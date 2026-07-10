/** Normalize a user-typed address into a navigable URL. Like Chrome's omnibox,
 *  a DOTLESS host (go/, jira/FOO-1) gets `http://` (intranet shortnames 302 to
 *  https anyway and lack a cert for the bare name); anything with a dot is https-first.
 */
export function normalizeTypedUrl(raw: string): string {
  const trimmed = raw.trim();
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  // Host = up to the first /?#, minus any port, so `localhost:3000/x` is dotless.
  const host = trimmed.split(/[/?#]/, 1)[0].replace(/:\d+$/, "");
  const dotless = host.length > 0 && !host.includes(".");
  return (dotless ? "http://" : "https://") + trimmed;
}
