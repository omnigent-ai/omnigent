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
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index))
    : [];
  const textArea = document.createElement("textarea");

  textArea.value = text;
  textArea.setAttribute("readonly", "");
  // Mark this as a transient clipboard helper. The iOS shell hides the native
  // Liquid Glass bar whenever an editable element is focused (it reads that as
  // the keyboard opening); focusing this textarea to run execCommand would
  // otherwise dismiss the bar for the rest of the session. Editable-focus
  // detection excludes elements carrying this marker.
  textArea.setAttribute("data-clipboard-helper", "");
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
    if (selection) {
      selection.removeAllRanges();
      for (const range of selectedRanges) {
        selection.addRange(range);
      }
    }
  }
}
