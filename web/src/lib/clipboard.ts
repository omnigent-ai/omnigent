/**
 * Copy `text` to the clipboard alongside an image, as one clipboard item so a
 * paste elsewhere gets both. Deliberately not `async`: `navigator.clipboard.write`
 * must be called synchronously, in the same tick as the user gesture that
 * triggered the copy — Safari and some Chromium builds reject a clipboard write
 * that happens after an `await` because it no longer looks user-initiated.
 * `loadImage`'s promise is handed to `ClipboardItem` directly, so the write
 * starts immediately and only the item's data resolves later.
 *
 * Falls back to `copyText` (text only) when the Async Clipboard API's write
 * path isn't available, or when the write itself rejects (e.g. `loadImage`
 * fails, or the browser refuses the image).
 */
export function copyTextWithImage(text: string, loadImage: () => Promise<Blob>): Promise<void> {
  if (typeof ClipboardItem === "undefined" || !navigator.clipboard?.write) {
    return copyText(text);
  }
  const data: Record<string, Blob | PromiseLike<Blob>> = {};
  if (text) data["text/plain"] = new Blob([text], { type: "text/plain" });
  data["image/png"] = loadImage();
  return navigator.clipboard.write([new ClipboardItem(data)]).catch(() => copyText(text));
}

export async function copyText(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through to the selected-textarea path when async clipboard is
      // unavailable at runtime, e.g. permission denied or a non-secure origin.
    }
  }

  if (copyTextWithExecCommand(text)) return;

  throw new Error("Clipboard API not available");
}

function copyTextWithExecCommand(text: string): boolean {
  if (
    typeof document === "undefined" ||
    typeof document.execCommand !== "function" ||
    !document.body
  ) {
    return false;
  }

  const selection = document.getSelection();
  const previouslyFocused =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index))
    : [];
  const textArea = document.createElement("textarea");

  textArea.value = text;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.top = "0";
  textArea.style.left = "0";
  textArea.style.width = "1px";
  textArea.style.height = "1px";
  textArea.style.padding = "0";
  textArea.style.border = "0";
  textArea.style.opacity = "0";
  textArea.style.pointerEvents = "none";

  const handleCopy = (event: ClipboardEvent) => {
    event.preventDefault();
    event.clipboardData?.setData("text/plain", text);
  };

  document.addEventListener("copy", handleCopy);
  document.body.appendChild(textArea);
  try {
    textArea.focus();
    textArea.select();
    textArea.selectionStart = 0;
    textArea.selectionEnd = textArea.value.length;

    return document.execCommand("copy");
  } finally {
    document.removeEventListener("copy", handleCopy);
    textArea.remove();
    if (previouslyFocused?.isConnected) {
      previouslyFocused.focus({ preventScroll: true });
    }
    if (selection) {
      selection.removeAllRanges();
      for (const range of selectedRanges) {
        selection.addRange(range);
      }
    }
  }
}
