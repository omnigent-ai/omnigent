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
// Copies the user asked for run before queued program copies, in order.
const explicitWrites: TerminalClipboardWrite[] = [];

export function isTerminalClipboardWritePending(): boolean {
  return writing;
}

export function queueTerminalClipboardWrite(request: TerminalClipboardWrite): void {
  pending = request;
  startDrain();
}

/**
 * Copy text the user explicitly chose. Queued program copies are dropped, and a
 * program copy already being written finishes first so it can't land later.
 * With nothing in flight the write starts at once, inside the user's gesture.
 */
export function writeExplicitTerminalClipboardText(text: string): Promise<boolean> {
  pending = null;
  return new Promise((resolve) => {
    explicitWrites.push({ text, isCurrent: () => true, onResult: resolve });
    startDrain();
  });
}

function startDrain(): void {
  if (writing) return;
  writing = true;
  void drain();
}

function takeNextWrite(): TerminalClipboardWrite | null {
  const explicit = explicitWrites.shift();
  if (explicit) return explicit;
  const request = pending;
  pending = null;
  return request;
}

async function drain(): Promise<void> {
  try {
    for (let request = takeNextWrite(); request !== null; request = takeNextWrite()) {
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
