/** React keys for items that can match on every identifying field (ADO replies share their
 *  thread's URL): the fields plus a repeat count stay unique and keep their value across polls. */
export function listKeys<T>(
  items: readonly T[],
  identity: (item: T) => readonly unknown[],
): string[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = JSON.stringify(identity(item));
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return `${base}#${repeat}`;
  });
}
