import type { ClipboardEvent } from "react";

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
// script-run edit. A hidden copy of the text before the caret measures its depth.
function revealCaretLine(textarea: HTMLTextAreaElement) {
  if (textarea.scrollHeight <= textarea.clientHeight) return;
  const probe = document.createElement("textarea");
  probe.className = textarea.className;
  probe.style.cssText = `position:absolute;visibility:hidden;height:0;min-height:0;width:${getComputedStyle(textarea).width}`;
  textarea.after(probe);
  const firstLineBottom = probe.scrollHeight;
  probe.value = textarea.value.slice(0, textarea.selectionEnd);
  const caretBottom = probe.scrollHeight;
  probe.remove();
  const scrollTop = Math.max(textarea.scrollTop, caretBottom - textarea.clientHeight);
  textarea.scrollTop = Math.min(scrollTop, caretBottom - firstLineBottom);
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
