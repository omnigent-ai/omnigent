import {
  formatKeybindingForAria,
  formatKeyStroke,
  isMacKeyboardPlatform,
  type KeybindingMode,
  type KeybindingRule,
} from "@/actions";

export const KEYBINDING_MODE_LABELS: Readonly<Record<KeybindingMode, string>> = {
  global: "Global",
  composer: "Composer",
  terminal: "Terminal",
  fileViewer: "File viewer",
  filesPanel: "Files panel",
  terminalsPanel: "Terminals panel",
  executionLogs: "Execution logs",
  markdownToc: "Markdown table of contents",
};

export function KeybindingSequence({
  sequence,
  emptyLabel = "Unbound",
}: {
  sequence: KeybindingRule["sequence"] | null;
  emptyLabel?: string;
}) {
  if (!sequence) return <span className="text-sm text-muted-foreground">{emptyLabel}</span>;
  const isMac = isMacKeyboardPlatform();
  return (
    <span
      role="img"
      className="inline-flex flex-wrap items-center justify-end gap-1"
      aria-label={`Keybinding ${formatKeybindingForAria(sequence, { isMac })}`}
    >
      <kbd className="inline-flex h-7 min-w-7 items-center justify-center rounded-lg border border-border bg-muted px-2 font-sans text-sm font-medium text-muted-foreground">
        {formatKeyStroke(sequence[0], { isMac })}
      </kbd>
    </span>
  );
}
