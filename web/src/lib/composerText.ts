import type { ClipboardEvent } from "react";
import { createTextFieldMirror } from "@/lib/textFieldMirror";

/**
 * Replace ``[start, end)`` (default: the selection) as typed input, so onChange
 * sees it. The editing commands also keep it on the undo stack; the fallback
 * covers environments without them.
 */
export function insertComposerText(
  textarea: HTMLTextAreaElement,
  text: string,
  start = textarea.selectionStart,
  end = textarea.selectionEnd,
) {
  textarea.setSelectionRange(start, end);
  const inserted =
    typeof document.execCommand === "function" &&
    (text || start === end
      ? document.execCommand("insertText", false, text)
      : document.execCommand("delete"));
  if (!inserted) {
    textarea.setRangeText(text, start, end, "end");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }
  revealCaretLine(textarea);
}

// Typing scrolls the caret's line into view, but Chromium skips that after a
// script-run edit. The mirror keeps the text after the caret so words wrap as in
// the field; at the end a space keeps a trailing blank line.
function revealCaretLine(textarea: HTMLTextAreaElement) {
  if (textarea.scrollHeight <= textarea.clientHeight) return;
  const style = getComputedStyle(textarea);
  const mirror = createTextFieldMirror(textarea, style);
  const caret = document.createElement("span");
  mirror.textContent = textarea.value.slice(0, textarea.selectionEnd);
  caret.textContent = textarea.value.slice(textarea.selectionEnd) || " ";
  mirror.append(caret);
  document.body.append(mirror);
  const caretRect = caret.getClientRects()[0];
  const mirrorTop = mirror.getBoundingClientRect().top;
  mirror.remove();
  if (!caretRect) return;
  // Lines share one height, so the middle of the caret's glyph box picks out its line.
  const lineHeight = parseFloat(style.lineHeight) || caretRect.height;
  const line = Math.floor((caretRect.top + caretRect.height / 2 - mirrorTop) / lineHeight);
  const lineTop = parseFloat(style.paddingTop) + line * lineHeight;
  const scrollTop = Math.max(textarea.scrollTop, lineTop + lineHeight - textarea.clientHeight);
  textarea.scrollTop = Math.min(scrollTop, lineTop);
}

// Longer pastes stay native: the insertText command handles each line
// separately and slows sharply past a few hundred lines.
const MAX_INSERTED_PASTE_LINES = 100;

/**
 * Insert a paste's plain text as typed input and claim the event. Chrome drops
 * a paste whose clipboard changes before it inserts, which happens when a
 * dictation or snippet tool restores the clipboard right after its paste; the
 * event's own data is read before that. Returns false when the paste stays native.
 */
export function insertPastedText(event: ClipboardEvent<HTMLTextAreaElement>): boolean {
  const text = event.clipboardData.getData("text/plain").replace(/\r\n?/g, "\n");
  if (!text || text.split("\n", MAX_INSERTED_PASTE_LINES + 1).length > MAX_INSERTED_PASTE_LINES) {
    return false;
  }
  event.preventDefault();
  insertComposerText(event.currentTarget, text);
  return true;
}
