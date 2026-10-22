/** A clipboard payload offered in more than one flavor. */
export interface RichTextPayload {
  /** Rendered HTML, taken by rich-text targets such as Slack or Google Docs. */
  html: string;
  /** Plain-text source, taken by editors, terminals, and code cells. */
  text: string;
}

export async function copyText(text: string): Promise<void> {
  if (await writeTextWithClipboardApi(text)) return;

  if (copyWithExecCommand({ text })) return;

  throw new Error("Clipboard API not available");
}

/**
 * Writes both flavors so the paste target picks the one it understands: a
 * rich-text editor keeps the formatting, everything else still gets `text`.
 */
export async function copyRichText({ html, text }: RichTextPayload): Promise<void> {
  if (await writeFlavorsWithClipboardApi({ html, text })) return;

  if (copyWithExecCommand({ html, text })) return;

  // Nothing could carry the HTML — copying the source beats copying nothing.
  // `writeText` is a narrower permission surface than `write`, so it can still
  // land after that one was refused; only the formatting is lost.
  if (await writeTextWithClipboardApi(text)) return;

  throw new Error("Clipboard API not available");
}

async function writeTextWithClipboardApi(text: string): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Async clipboard can be unavailable at runtime, e.g. permission denied or
    // a non-secure origin; the caller falls back to the selected-textarea path.
    return false;
  }
}

/**
 * Called before this function's first `await` resolves, so the `write` lands
 * inside the click's user-activation window — deferring it loses the
 * permission.
 */
async function writeFlavorsWithClipboardApi({ html, text }: RichTextPayload): Promise<boolean> {
  if (
    typeof navigator === "undefined" ||
    !navigator.clipboard?.write ||
    typeof ClipboardItem !== "function"
  ) {
    return false;
  }
  try {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      }),
    ]);
    return true;
  } catch {
    // Multi-flavor `write` can be unavailable or permission gated where the
    // plain-text paths still succeed.
    return false;
  }
}

function copyWithExecCommand({ html, text }: { html?: string; text: string }): boolean {
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
    if (html !== undefined) event.clipboardData?.setData("text/html", html);
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
