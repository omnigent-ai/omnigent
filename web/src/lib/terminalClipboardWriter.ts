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
// What the user copied while a program copy was in flight; that copy's result is dropped.
interface ExplicitCopy {
  text: string;
  isAllowed: () => boolean;
}
let supersededBy: ExplicitCopy | null = null;

export function isTerminalClipboardWritePending(): boolean {
  return writing;
}

export function queueTerminalClipboardWrite(request: TerminalClipboardWrite): void {
  pending = request;
  if (writing) return;
  writing = true;
  void drain();
}

/**
 * Copy text the user chose, starting at once so it stays inside their gesture.
 * Queued program copies are dropped and one in flight is superseded.
 * `isAllowed` is checked again before the text is restored over that copy.
 */
export async function writeExplicitTerminalClipboardText(
  text: string,
  isAllowed: () => boolean,
): Promise<boolean> {
  pending = null;
  if (writing) supersededBy = { text, isAllowed };
  try {
    await copyText(text);
    return true;
  } catch {
    return false;
  }
}

function takeSupersedingCopy(): ExplicitCopy | null {
  const userCopy = supersededBy;
  supersededBy = null;
  return userCopy;
}

function takePending(): TerminalClipboardWrite | null {
  const request = pending;
  pending = null;
  return request;
}

async function drain(): Promise<void> {
  try {
    for (let request = takePending(); request !== null; request = takePending()) {
      if (!request.isCurrent()) continue;
      supersededBy = null;
      let copied = false;
      try {
        // oxlint-disable-next-line no-await-in-loop
        await copyText(request.text);
        copied = true;
      } catch {
        // Browser gesture/permission failures remain retryable by the caller.
      }
      const userCopy = takeSupersedingCopy();
      if (userCopy !== null) {
        // The program copy may have landed after the user's, so write theirs again.
        if (copied && userCopy.isAllowed()) {
          // oxlint-disable-next-line no-await-in-loop
          await copyText(userCopy.text).catch(() => {});
        }
        continue;
      }
      if (request.isCurrent()) request.onResult(copied);
    }
  } finally {
    writing = false;
    supersededBy = null;
  }
}
