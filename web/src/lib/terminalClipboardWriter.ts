import { copyText } from "./clipboard";

interface TerminalClipboardWrite {
  text: string;
  isCurrent: () => boolean;
  onResult: (copied: boolean) => void;
}

// The clipboard is shared across terminal views, including unmounted ones
// whose browser writes are still in flight. Only the latest queued text matters.
let writing = false;
let pending: TerminalClipboardWrite | null = null;

export function isTerminalClipboardWritePending(): boolean {
  return writing;
}

export function queueTerminalClipboardWrite(request: TerminalClipboardWrite): void {
  pending = request;
  if (writing) return;
  writing = true;
  void drain();
}

async function drain(): Promise<void> {
  try {
    while (pending !== null) {
      const request = pending;
      pending = null;
      if (!request.isCurrent()) continue;
      let copied = false;
      try {
        // oxlint-disable-next-line no-await-in-loop
        await copyText(request.text);
        copied = true;
      } catch {
        // Browser gesture/permission failures remain retryable by the caller.
      }
      if (request.isCurrent()) request.onResult(copied);
    }
  } finally {
    writing = false;
  }
}
