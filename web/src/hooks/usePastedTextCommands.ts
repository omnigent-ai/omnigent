// ⌘K's "Insert pasted-text-N.txt as Text", one per pasted attachment the draft still holds:
// the same action as the chip's own Insert as text.

import { ClipboardTypeIcon } from "lucide-react";
import { useMemo } from "react";
import type { AttachmentAction } from "@/components/ComposerAttachments";
import { usePaletteCommands, type PaletteCommand } from "@/store/paletteCommands";
import type { PastedTextAttachments } from "./usePastedTextAttachments";

/** A pasted attachment chip's Insert as text, shown as the chip's own action. */
export type PastedTextChipAction = AttachmentAction;

type RestoreSource = Pick<PastedTextAttachments, "chips" | "insertAsText" | "canRestore">;

export function pastedLinesLabel(lines: number): string {
  return lines === 1 ? "1 line" : `${lines.toLocaleString()} lines`;
}

/** A pasted attachment's chip action, the same Insert as text as its ⌘K command. */
export function pastedTextAction(
  { chips, insertAsText, canRestore }: RestoreSource,
  file: File,
): PastedTextChipAction | undefined {
  const chip = chips.find((each) => each.file === file);
  if (!chip || !canRestore) return undefined;
  return {
    label: "Insert as text",
    ariaLabel: `Insert ${file.name} as text`,
    detail: pastedLinesLabel(chip.lines),
    onSelect: () => void insertAsText(file),
  };
}

const NO_COMMANDS: PaletteCommand[] = [];

export function usePastedTextCommands(
  source: string,
  { chips, insertAsText, canRestore }: RestoreSource,
): void {
  const commands = useMemo<PaletteCommand[]>(
    () =>
      canRestore
        ? chips.map(({ file, lines }) => ({
            id: `insert-pasted-text:${file.name}`,
            label: `Insert ${file.name} as Text`,
            description: pastedLinesLabel(lines),
            icon: ClipboardTypeIcon,
            keywords: ["paste", "pasted text", "attachment", "inline", "restore"],
            // It focuses the message, so it runs once the palette has closed.
            movesFocus: true,
            run: () => void insertAsText(file),
          }))
        : NO_COMMANDS,
    [chips, insertAsText, canRestore],
  );
  usePaletteCommands(source, "Message", commands);
}
