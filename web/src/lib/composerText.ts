import {
  isComposerTextarea,
  type ComposerInsertOptions,
  type ComposerPasteEvent,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import { createTextFieldMirror } from "@/lib/textFieldMirror";

/**
 * Replace ``[start, end)`` (default: the selection) as typed input, so onChange
 * sees it. The editing commands also keep it on the undo stack; the fallback
 * covers environments without them. Returns false when the input is locked and
 * nothing changed. `separateUndo` keeps the edit its own undo step, apart from typing.
 */
export function insertComposerText(
  target: ComposerTextTarget,
  text: string,
  start = target.selectionStart,
  end = target.selectionEnd,
  options?: ComposerInsertOptions,
): boolean {
  if (!isComposerTextarea(target)) {
    const inserted = target.insertText(text, start, end, options);
    if (inserted) target.revealCaret();
    return inserted;
  }
  const textarea = target;
  if (textarea.readOnly || textarea.disabled) return false;
  // Chromium merges an insert into the typing around it unless the selection moved since.
  if (options?.separateUndo) breakTypingGroup(textarea);
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
  if (options?.separateUndo) breakTypingGroup(textarea);
  revealCaretLine(textarea);
  return true;
}

/** Move the selection away and back, which ends the browser's open typing undo group. */
function breakTypingGroup(textarea: HTMLTextAreaElement) {
  const { selectionStart, selectionEnd, selectionDirection } = textarea;
  // An empty field has nowhere to move the caret, so its typing group can't be broken here.
  const elsewhere = selectionEnd === 0 ? Math.min(1, textarea.value.length) : 0;
  textarea.setSelectionRange(elsewhere, elsewhere);
  textarea.setSelectionRange(selectionStart, selectionEnd, selectionDirection ?? undefined);
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
 * event's own data is read before that. Returns true only when the text was
 * inserted; false means the paste stays native, or a locked input cancelled it.
 */
export function insertPastedText(event: ComposerPasteEvent): boolean {
  const text = event.clipboardData.getData("text/plain").replace(/\r\n?/g, "\n");
  if (!text || text.split("\n", MAX_INSERTED_PASTE_LINES + 1).length > MAX_INSERTED_PASTE_LINES) {
    return false;
  }
  event.preventDefault();
  return insertComposerText(event.currentTarget, text);
}
