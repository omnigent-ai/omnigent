/*!
 * This helper is adapted from Dinotty's terminalInputCore.ts.
 *
 * MIT License
 * Copyright (c) 2026 ChenXi
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

export interface TerminalTextareaSnapshot {
  value: string;
  selectionStart: number;
  selectionEnd: number;
}

function codePointOffset(value: string, utf16Offset: number): number {
  return Array.from(value.slice(0, Math.max(0, utf16Offset))).length;
}

function moveTerminalCursor(from: number, to: number, applicationCursor: boolean): string {
  if (from === to) return "";

  const direction = to < from ? "D" : "C";
  const sequence = `\x1b${applicationCursor ? "O" : "["}${direction}`;
  return sequence.repeat(Math.abs(to - from));
}

export function normalizeTerminalTextareaSelection(
  before: TerminalTextareaSnapshot,
  after: TerminalTextareaSnapshot,
  inputData: string | null,
): TerminalTextareaSnapshot {
  if (!inputData || after.selectionStart !== after.selectionEnd) return after;

  const insertionStart = after.selectionEnd;
  const insertionEnd = insertionStart + inputData.length;
  const insertedAtSelection = after.value.slice(insertionStart, insertionEnd) === inputData;
  const valueWithoutInsertion =
    after.value.slice(0, insertionStart) + after.value.slice(insertionEnd);

  return insertedAtSelection && valueWithoutInsertion === before.value
    ? {
        ...after,
        selectionStart: insertionEnd,
        selectionEnd: insertionEnd,
      }
    : after;
}

export function terminalTextareaEdit(
  before: TerminalTextareaSnapshot,
  after: TerminalTextareaSnapshot,
  applicationCursor = false,
): string {
  const cursorBefore = codePointOffset(before.value, before.selectionEnd);
  const desiredCursor = codePointOffset(after.value, after.selectionEnd);

  if (before.value === after.value) {
    return moveTerminalCursor(cursorBefore, desiredCursor, applicationCursor);
  }

  const oldText = Array.from(before.value);
  const newText = Array.from(after.value);
  let commonPrefixLength = 0;
  while (
    commonPrefixLength < oldText.length &&
    commonPrefixLength < newText.length &&
    oldText[commonPrefixLength] === newText[commonPrefixLength]
  ) {
    commonPrefixLength += 1;
  }

  let commonSuffixLength = 0;
  while (
    commonSuffixLength < oldText.length - commonPrefixLength &&
    commonSuffixLength < newText.length - commonPrefixLength &&
    oldText[oldText.length - 1 - commonSuffixLength] ===
      newText[newText.length - 1 - commonSuffixLength]
  ) {
    commonSuffixLength += 1;
  }

  const oldEditEnd = oldText.length - commonSuffixLength;
  const insertedText = newText
    .slice(commonPrefixLength, newText.length - commonSuffixLength)
    .join("");
  const cursorAfterEdit = commonPrefixLength + Array.from(insertedText).length;

  return [
    moveTerminalCursor(cursorBefore, oldEditEnd, applicationCursor),
    "\x7f".repeat(oldEditEnd - commonPrefixLength),
    insertedText,
    moveTerminalCursor(cursorAfterEdit, desiredCursor, applicationCursor),
  ].join("");
}
