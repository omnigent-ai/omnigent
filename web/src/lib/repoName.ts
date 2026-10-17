/** Repository display name from its URL: the last path segment minus a trailing ``.git``
 * (the server's clone-directory rule, so chip labels match the workspace the session gets);
 * ``null`` when underivable. */
export function deriveRepoName(url: string): string | null {
  const t = url.trim().replace(/\/+$/, "");
  if (t === "") return null;
  const last = t.split(/[/:]/).pop() ?? "";
  const name = last.endsWith(".git") ? last.slice(0, -4) : last;
  return name === "" ? null : name;
}
