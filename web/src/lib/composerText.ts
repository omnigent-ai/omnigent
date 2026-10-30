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
  if (inserted) return;
  textarea.setRangeText(text, start, end, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}
