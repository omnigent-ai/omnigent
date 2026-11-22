// A large plain-text paste becomes a text attachment instead of flooding the draft. These are
// the rules for when, what file it becomes, and how a remembered offset follows later edits.

/** A paste with at least this many lines, or this many characters, becomes an attachment. */
export const PASTE_ATTACHMENT_THRESHOLD = { lines: 40, characters: 4000 } as const;

/** The paste's plain text, with Windows and classic Mac line endings normalized. */
export function pastedPlainText(data: Pick<DataTransfer, "getData">): string {
  return data.getData("text/plain").replace(/\r\n?/g, "\n");
}

export function lineCount(text: string): number {
  if (text === "") return 0;
  let lines = 1;
  for (let index = text.indexOf("\n"); index >= 0; index = text.indexOf("\n", index + 1)) {
    lines += 1;
  }
  return lines;
}

export function exceedsPasteThreshold(text: string): boolean {
  return (
    text.length >= PASTE_ATTACHMENT_THRESHOLD.characters ||
    lineCount(text) >= PASTE_ATTACHMENT_THRESHOLD.lines
  );
}

/** The attachment a large paste becomes; `sequence` keeps several pastes' names apart. */
export function pastedTextFile(text: string, sequence: number): File {
  return new File([text], `pasted-text-${sequence}.txt`, { type: "text/plain" });
}

/**
 * Where `offset` ends up after `before` became `after`. Text before the change keeps its
 * place, text after it shifts by the change's length, and an offset inside the replaced
 * span moves to where the change starts.
 */
export function mapOffsetThroughEdit(offset: number, before: string, after: string): number {
  if (before === after) return offset;
  let prefix = 0;
  const limit = Math.min(before.length, after.length);
  while (prefix < limit && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  if (offset <= prefix) return offset;
  if (offset >= before.length - suffix) return offset + (after.length - before.length);
  return prefix;
}

/** `value` with `text` inserted at `offset`, clamped into range; never removes anything. */
export function insertAtOffset(value: string, offset: number, text: string): string {
  const at = Math.max(0, Math.min(offset, value.length));
  return value.slice(0, at) + text + value.slice(at);
}
