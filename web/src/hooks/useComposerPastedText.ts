// A composer's large-paste behavior on the CodeMirror input: a large text paste becomes an
// attachment with a way back (⌘Z right after it, and Insert as text on its chip, its hint and
// ⌘K). The textarea keeps the browser's own paste. Each owner says what its draft is.

import {
  isComposerTextarea,
  type ComposerKeyboardEvent,
  type ComposerPasteEvent,
} from "@/components/composer/engine/composerInput";
import { clipboardFiles } from "@/lib/attachments";
import { hasCommandModifier } from "@/lib/hotkeys";
import {
  pastedTextAction,
  usePastedTextCommands,
  type PastedTextChipAction,
} from "./usePastedTextCommands";
import {
  usePastedTextAttachments,
  type PastedTextAttachments,
  type PastedTextAttachmentsOptions,
} from "./usePastedTextAttachments";

export interface ComposerPastedTextOptions extends PastedTextAttachmentsOptions {
  /** Names this composer's ⌘K Insert … as Text commands. */
  commandSource: string;
  /** The composer's own paste handling (files, images, plain text) for pastes this leaves. */
  onOtherPaste: (event: ComposerPasteEvent) => void;
}

export interface ComposerPastedText extends PastedTextAttachments {
  onPaste: (event: ComposerPasteEvent) => void;
  /** ⌘Z right after a paste became an attachment; true when it handled the key. */
  onKeyDown: (event: ComposerKeyboardEvent) => boolean;
  /** A pasted attachment chip's Insert as text. */
  actionFor: (file: File) => PastedTextChipAction | undefined;
}

export function useComposerPastedText({
  commandSource,
  onOtherPaste,
  ...attachments
}: ComposerPastedTextOptions): ComposerPastedText {
  const pastedText = usePastedTextAttachments(attachments);
  usePastedTextCommands(commandSource, pastedText);
  const { locked } = attachments;

  const onPaste = (event: ComposerPasteEvent) => {
    // A locked draft takes no paste at all: no text, no attachment, not the owner's either.
    if (locked) {
      event.preventDefault();
      return;
    }
    // The textarea keeps today's paste, so switching back restores it exactly.
    if (
      !isComposerTextarea(event.currentTarget) &&
      clipboardFiles(event.clipboardData).length === 0 &&
      pastedText.offerPaste(event.currentTarget, event.clipboardData)
    ) {
      event.preventDefault();
      return;
    }
    onOtherPaste(event);
  };

  const onKeyDown = (event: ComposerKeyboardEvent): boolean => {
    // Undo right after a large paste became an attachment puts the text back instead.
    if (
      event.key.toLowerCase() === "z" &&
      hasCommandModifier(event) &&
      !event.shiftKey &&
      !event.altKey &&
      pastedText.undoLatestPaste()
    ) {
      event.preventDefault();
      return true;
    }
    return false;
  };

  return {
    ...pastedText,
    onPaste,
    onKeyDown,
    actionFor: (file) => pastedTextAction(pastedText, file),
  };
}
