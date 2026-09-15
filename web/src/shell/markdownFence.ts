// A code fence that safely wraps `body`: one more backtick than the longest
// run inside it, and at least three. Shared by the code-block markdown
// serializer and the mermaid preview so the two can't drift.
export function fenceForBody(body: string): string {
  const ticks = [...body.matchAll(/`+/g)].reduce((n, m) => Math.max(n, m[0].length), 2) + 1;
  return "`".repeat(ticks);
}
