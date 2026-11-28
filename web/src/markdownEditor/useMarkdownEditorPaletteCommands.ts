// ⌘K rows for the Markdown editor the person last worked in: its formats and heading levels, and
// where it has them, attach, the Write/Preview switch and its submit. A row runs after the
// palette closes and hands focus back to the editor, so the command acts on the selection it had.

import { useId, useMemo } from "react";
import {
  EyeIcon,
  HeadingIcon,
  type LucideIcon,
  PaperclipIcon,
  PencilIcon,
  PilcrowIcon,
  SendIcon,
} from "lucide-react";

import { createLastFocusedScope } from "@/hooks/lastFocusedScope";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import {
  HEADING_LEVELS,
  type HeadingLevel,
  MARKDOWN_FORMATS,
  type MarkdownFormat,
  TOGGLE_PREVIEW_CHORD,
} from "./markdownFormatting";

export type MarkdownComposerMode = "write" | "preview";

// The editor the person focused last; only it offers rows, so two open editors don't compete.
const editors = createLastFocusedScope();

export interface MarkdownEditorPaletteOptions {
  disabled: boolean;
  /** The formats offered, in the table's order; every format when unset. */
  formats?: readonly MarkdownFormat[];
  runFormat: (format: MarkdownFormat) => void;
  /** Offers a row per heading level, for an editor with a heading picker. */
  setHeadingLevel?: (level: HeadingLevel) => void;
  attach?: (() => void) | null;
  /** The Write/Preview switch, for an editor that has both. */
  preview?: { mode: MarkdownComposerMode; toggle: () => void };
  /** The submit row: "Save description", "Comment", …. */
  submit?: { label: string; run: () => void };
}

/** Register the rows; returns the focus handler the editor's frame calls. */
export function useMarkdownEditorPaletteCommands(
  options: MarkdownEditorPaletteOptions,
): () => void {
  const editorId = useId();
  const { active, markFocused } = editors.useLastFocused();

  const { disabled, formats, runFormat, setHeadingLevel, attach, preview, submit } = options;
  const mode = preview?.mode ?? "write";
  const togglePreview = preview?.toggle;
  const submitLabel = submit?.label;
  const runSubmit = submit?.run;
  const formatKey = formats?.join() ?? null;
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!active || disabled) return [];
    const row = (
      id: string,
      label: string,
      icon: LucideIcon,
      keywords: string[],
      run: () => void,
    ) => ({
      id: `markdown-editor:${editorId}:${id}`,
      label,
      icon,
      keywords,
      movesFocus: true,
      run,
    });
    const offered = formatKey === null ? null : new Set(formatKey.split(","));
    return [
      // Formats act on Write's selection, so Preview offers only the switch back and submit.
      ...(mode === "write"
        ? MARKDOWN_FORMATS.filter((command) => !offered || offered.has(command.format)).map(
            (command) => ({
              ...row(
                command.format,
                command.label,
                command.icon,
                ["format", ...command.keywords],
                () => runFormat(command.format),
              ),
              nativeShortcut: command.chord,
            }),
          )
        : []),
      ...(mode === "write" && setHeadingLevel
        ? HEADING_LEVELS.map((heading) =>
            row(
              `heading-${heading.level}`,
              heading.label,
              heading.level === 0 ? PilcrowIcon : HeadingIcon,
              ["format", "heading", ...heading.keywords],
              () => setHeadingLevel(heading.level),
            ),
          )
        : []),
      ...(attach
        ? [row("attach", "Attach Images or Videos…", PaperclipIcon, ["upload", "file"], attach)]
        : []),
      ...(togglePreview
        ? [
            {
              ...(mode === "write"
                ? row("mode", "Show Preview", EyeIcon, ["markdown", "render"], togglePreview)
                : row("mode", "Show Write", PencilIcon, ["markdown", "edit"], togglePreview)),
              nativeShortcut: TOGGLE_PREVIEW_CHORD,
            },
          ]
        : []),
      ...(submitLabel && runSubmit
        ? [row("submit", submitLabel, SendIcon, ["save", "post", "submit"], runSubmit)]
        : []),
    ];
  }, [
    active,
    disabled,
    editorId,
    formatKey,
    mode,
    runFormat,
    setHeadingLevel,
    attach,
    togglePreview,
    submitLabel,
    runSubmit,
  ]);
  usePaletteCommands(`markdown-editor:${editorId}`, "Formatting", commands);

  return markFocused;
}
