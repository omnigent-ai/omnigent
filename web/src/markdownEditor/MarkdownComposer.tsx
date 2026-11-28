// The pull request page's Markdown composer, as GitHub has it: Write is the Markdown source in a
// plain textarea, Preview renders it, and a toolbar, keys and ⌘K rows insert Markdown syntax.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { isMacPlatform } from "@/lib/hotkeys";
import { chordKeys, chordMatches } from "@/lib/shortcuts/chord";
import { EDITOR_KEYS_ATTRIBUTE } from "@/lib/shortcuts/editorOwnedChords";
import { cn } from "@/lib/utils";

import {
  applyUndoableTextEdit,
  inLineEndingsOf,
  insertText,
  MARKDOWN_FORMATS,
  type MarkdownFormat,
  markdownLinkPaste,
  replaceKeepingSelection,
  TOGGLE_PREVIEW_CHORD,
  textareaEdit,
} from "./markdownFormatting";
import { type CompletionListbox, MarkdownCompletionPopover } from "./MarkdownCompletionPopover";
import { MarkdownToolIcon } from "./MarkdownToolIcon";
import { applyMarkdownFormat, handleMarkdownTextareaKey } from "./markdownTextareaKeys";
import {
  applyUploadToText,
  ATTACHMENT_ACCEPT,
  type AttachmentUploadContext,
  deliverUpload,
  recordSettledUpload,
  registerUploadHost,
  resolveSettledUploads,
  uploadAttachments,
  uploadPlaceholderMarkdown,
  uploadPlaceholderPattern,
} from "./pullRequestAttachments";
import {
  type MarkdownComposerMode,
  useMarkdownEditorPaletteCommands,
} from "./useMarkdownEditorPaletteCommands";
import { useMarkdownCompletion } from "./useMarkdownCompletion";

/** Where a composer's attachments upload, and how their progress reaches its draft. */
export interface MarkdownComposerAttachments extends AttachmentUploadContext {
  /** Change the text of the draft that is open now; an upload's owner keeps this from its start. */
  updateDraftText: (change: (text: string) => string) => void;
  onUploadingChange: (delta: number) => void;
  onAttachmentError: (error: string) => void;
  /** A new batch of files is about to be checked: the last batch's messages no longer apply. */
  onAttachmentBatch?: () => void;
}

export interface MarkdownComposerProps {
  value: string;
  onChange: (value: string) => void;
  /** ⌘↵, the ⌘K row and the caller's own button all submit through the caller's guards. */
  onSubmit: () => void;
  onCancel?: () => void;
  /** The ⌘K submit row's label, such as "Save Description". */
  submitLabel: string;
  /** What ⌘↵ does, for the footer's hint: "comment", "reply", "save". */
  submitVerb: string;
  label: string;
  placeholder?: string;
  disabled?: boolean;
  autoFocus?: boolean;
  /** Bumped to move the caret to the end of the text, as opening the composer on request does. */
  focusRequest?: number;
  size: "document" | "comment";
  /** The rendered body for Preview; built by the caller so it matches the posted Markdown. */
  preview: ReactNode;
  /** Where attachments upload; null leaves attaching out. */
  attachments: MarkdownComposerAttachments | null;
  /** Why attaching isn't available here, shown in the footer instead of how to attach. */
  attachmentsUnavailableReason?: string;
  /** Under the editor, inside the frame: notices and the caller's actions. */
  children?: ReactNode;
  /** The footer's actions, after the hints, such as Cancel and Comment. */
  footerActions?: ReactNode;
}

const SIZES = {
  // Min wins over max in a short pane, so the editor never collapses below a few lines.
  document: "min-h-32 max-h-[max(8rem,min(28rem,calc(100cqh-14rem)))]",
  comment: "min-h-[4.5rem] max-h-[max(4.5rem,min(13rem,50cqh))]",
} as const;

// Dragging the editor's edge sets an inline height, which may go past where typing stops growing.
const DRAGGED_SIZES = {
  document: "[&[style*=height]]:max-h-[max(8rem,min(48rem,calc(100cqh-10rem)))]",
  comment: "[&[style*=height]]:max-h-[max(4.5rem,min(24rem,60cqh))]",
} as const;

const TOOLBAR_FORMATS = MARKDOWN_FORMATS.filter((command) => command.toolbar);

const withLineFeeds = (text: string) => text.replace(/\r\n?/g, "\n");

export function MarkdownComposer({
  value,
  onChange,
  onSubmit,
  onCancel,
  submitLabel,
  submitVerb,
  label,
  placeholder,
  disabled = false,
  autoFocus = false,
  focusRequest = 0,
  size,
  preview,
  attachments,
  attachmentsUnavailableReason,
  children,
  footerActions,
}: MarkdownComposerProps) {
  const mac = isMacPlatform();
  const [mode, setMode] = useState<MarkdownComposerMode>("write");
  // The textarea's text exactly as the browser has it. It is never set from code while typing:
  // a reset would move the caret and empty the native undo history. Owners' draft stores
  // re-render a moment later, and the draft can differ (resolved uploads, the body's line endings).
  const [text, setText] = useState(() => withLineFeeds(value));
  const [ownerValue, setOwnerValue] = useState(value);
  // The last draft this composer handed its owner or took from it; only a different one is a change.
  const [emitted, setEmitted] = useState(value);
  // The body as it loaded, whose line endings an edit keeps.
  const [source] = useState(value);
  if (value !== ownerValue) {
    // The owner changed the draft itself, as when a post clears it or another composer of the
    // same draft wrote to it. A draft that is only this text with its uploads finished (another
    // composer of the draft took the upload) leaves the text, and its history, alone.
    setOwnerValue(value);
    if (value !== emitted) {
      // Adopted, so it is the draft to compare the owner's next value with, even if it returns
      // to text this composer emitted earlier.
      setEmitted(value);
      const shownAsDraft = withLineFeeds(resolveSettledUploads(text));
      if (withLineFeeds(value) !== shownAsDraft) setText(withLineFeeds(value));
    }
  }
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // `@` and `#` completion, where the pull request around this editor offers it.
  const completion = useMarkdownCompletion(textareaRef);
  const [completionListbox, setCompletionListbox] = useState<CompletionListbox | null>(null);
  const completing = completion.token !== null;
  const previewRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const latest = useRef({ attachments, onChange, onSubmit });
  // Whether the browser has edits of this textarea to undo, which setting its text would lose.
  const editedHere = useRef(false);
  latest.current = { attachments, onChange, onSubmit };

  // Focus on mount when asked, and again on each focus request, never when merely re-rendered.
  // A request made while Preview shows goes back to Write first, where the caret can go.
  const focusedRequest = useRef(autoFocus ? -1 : 0);
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea || focusedRequest.current === focusRequest) return;
    if (mode !== "write") {
      setMode("write");
      return;
    }
    focusedRequest.current = focusRequest;
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    textarea.scrollIntoView?.({ block: "nearest" });
  }, [focusRequest, mode]);

  /** Hand the owner `shown` as its draft: settled uploads resolved, in the body's line endings. */
  const emit = useCallback(
    (shown: string) => {
      // Undo can bring back the placeholder of an upload that has since settled; the draft keeps
      // the upload's result, so a placeholder is never posted or previewed.
      const draft = inLineEndingsOf(source, resolveSettledUploads(shown));
      setEmitted(draft);
      latest.current.onChange(draft);
    },
    [source],
  );
  const change = useCallback(
    (next: string) => {
      setText(next);
      emit(next);
    },
    [emit],
  );

  const { dismiss: dismissCompletion } = completion;
  const switchMode = useCallback(
    (next: MarkdownComposerMode) => {
      dismissCompletion();
      setMode(next);
      requestAnimationFrame(() => {
        if (next === "write") textareaRef.current?.focus();
        else previewRef.current?.focus();
      });
    },
    [dismissCompletion],
  );
  const toggleMode = useCallback(
    () => switchMode(mode === "write" ? "preview" : "write"),
    [mode, switchMode],
  );

  const runFormat = useCallback((format: MarkdownFormat) => {
    const textarea = textareaRef.current;
    if (textarea) applyMarkdownFormat(textarea, format);
  }, []);

  /** Upload files at the selection, each as a placeholder that turns into its attachment. */
  const attach = useCallback((files: File[]) => {
    const owner = latest.current.attachments;
    if (!owner || files.length === 0) return;
    owner.onAttachmentBatch?.();
    // The draft that started an upload gets its result, even after this composer closed or a
    // newer draft opened: its callbacks are the ones current when the upload began.
    const settle = (id: string, markdown: string | null) => {
      owner.onUploadingChange(-1);
      recordSettledUpload(id, markdown);
      if (!deliverUpload(id, markdown))
        owner.updateDraftText((draftText) => applyUploadToText(draftText, id, markdown));
    };
    uploadAttachments(files, owner, {
      refuse: owner.onAttachmentError,
      start: (id, fileName) => {
        owner.onUploadingChange(1);
        const textarea = textareaRef.current;
        const uploading = uploadPlaceholderMarkdown(id, fileName);
        if (textarea)
          applyUndoableTextEdit(textarea, insertText(textareaEdit(textarea), uploading));
        else owner.updateDraftText((draftText) => draftText + uploading);
      },
      finish: settle,
      fail: (id, failure) => {
        owner.onAttachmentError(failure);
        settle(id, null);
      },
    });
  }, []);

  // While mounted, this composer applies finished uploads whose placeholders it shows.
  useEffect(
    () =>
      registerUploadHost((id, markdown) => {
        const textarea = textareaRef.current;
        if (!textarea || !uploadPlaceholderPattern(id).test(textarea.value)) return false;
        if (textarea.ownerDocument.activeElement === textarea) {
          // Where someone is typing, the result is one undo step that keeps their caret.
          replaceKeepingSelection(textarea, applyUploadToText(textarea.value, id, markdown));
        } else if (editedHere.current) {
          // Off the keyboard (in Preview, say) the draft takes the result now; the text keeps its
          // undo history and takes the result, as one undo step, when it's focused again.
          emit(textarea.value);
        } else {
          // Text nobody edited in this composer has no history to keep, so it shows the result.
          change(applyUploadToText(textarea.value, id, markdown));
        }
        return true;
      }),
    [change, emit],
  );

  const attachFrom = (transfer: DataTransfer | null): boolean => {
    const files = Array.from(transfer?.files ?? []);
    if (!latest.current.attachments || files.length === 0) return false;
    attach(files);
    return true;
  };

  const markFocused = useMarkdownEditorPaletteCommands({
    disabled,
    runFormat,
    attach: attachments ? () => fileInputRef.current?.click() : null,
    preview: { mode, toggle: toggleMode },
    submit: { label: submitLabel, run: () => latest.current.onSubmit() },
  });

  return (
    <Tabs
      value={mode}
      onValueChange={(next) => {
        if (next === "write" || next === "preview") switchMode(next);
      }}
      className={cn(
        "min-h-0 gap-0 overflow-hidden rounded-[10px] border border-input bg-background transition-[border-color,box-shadow]",
        "focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/20",
        disabled && "opacity-70",
      )}
      // ⌘↵ submits this composer, whatever it holds, and never accepts a pending agent request.
      {...{ [OWNS_COMMAND_ENTER_ATTRIBUTE]: "", [EDITOR_KEYS_ATTRIBUTE]: "rich" }}
      onFocus={markFocused}
    >
      <div className="flex min-h-[38px] shrink-0 flex-wrap items-center justify-between gap-x-2 border-b border-border bg-muted/40 px-1.5">
        <TabsList variant="line" aria-label={`${label} mode`} className="h-[38px] gap-0.5 p-0">
          <TabsTrigger
            value="write"
            className="h-full flex-none rounded-none px-2.5 text-xs font-semibold"
          >
            Write
          </TabsTrigger>
          <TabsTrigger
            value="preview"
            className="h-full flex-none rounded-none px-2.5 text-xs font-semibold"
          >
            Preview
          </TabsTrigger>
        </TabsList>
        {mode === "write" && <MarkdownToolbar disabled={disabled} onFormat={runFormat} />}
      </div>
      {/* Write stays mounted behind Preview, so the textarea keeps its native undo history. */}
      <TabsContent value="write" forceMount className="min-h-0 data-[state=inactive]:hidden">
        <textarea
          ref={textareaRef}
          aria-label={label}
          value={text}
          disabled={disabled}
          placeholder={placeholder}
          spellCheck
          {...(completing && {
            role: "combobox",
            "aria-autocomplete": "list" as const,
            "aria-expanded": true,
            "aria-controls": completionListbox?.id,
            "aria-activedescendant": completionListbox?.activeId,
          })}
          onChange={(event) => {
            editedHere.current = true;
            change(event.target.value);
            completion.refresh();
          }}
          // A click or a caret move may leave the token being completed, or reach another.
          onSelect={completion.refresh}
          onBlur={completion.dismiss}
          onFocus={(event) => {
            // Uploads that settled while it was off the keyboard replace their placeholders.
            const textarea = event.currentTarget;
            replaceKeepingSelection(textarea, resolveSettledUploads(textarea.value));
          }}
          onKeyDown={(event) => {
            // While suggestions show, their keys come first; every other key is the editor's.
            if (completion.handleKey(event.nativeEvent)) {
              event.preventDefault();
              return;
            }
            const handled = handleMarkdownTextareaKey(event.nativeEvent, event.currentTarget, {
              mac,
              onSubmit: () => latest.current.onSubmit(),
              onCancel,
              onTogglePreview: toggleMode,
            });
            if (handled) event.preventDefault();
          }}
          onPaste={(event) => {
            if (attachFrom(event.clipboardData)) {
              event.preventDefault();
              return;
            }
            const link = markdownLinkPaste(
              textareaEdit(event.currentTarget),
              event.clipboardData.getData("text/plain"),
            );
            if (!link) return;
            event.preventDefault();
            applyUndoableTextEdit(event.currentTarget, link);
          }}
          onDragOver={(event) => {
            if (attachments && event.dataTransfer.types.includes("Files")) event.preventDefault();
          }}
          onDrop={(event) => {
            if (attachFrom(event.dataTransfer)) event.preventDefault();
          }}
          className={cn(
            SIZES[size],
            DRAGGED_SIZES[size],
            "field-sizing-content block w-full resize-y overflow-y-auto bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed",
          )}
        />
        {completing && (
          <MarkdownCompletionPopover
            completion={completion}
            textareaRef={textareaRef}
            onListbox={setCompletionListbox}
          />
        )}
      </TabsContent>
      <TabsContent
        value="preview"
        tabIndex={-1}
        onKeyDown={(event) => {
          const keys = event.nativeEvent;
          if (chordMatches(TOGGLE_PREVIEW_CHORD, keys, mac)) {
            event.preventDefault();
            toggleMode();
          } else if (chordMatches({ mod: true, key: "Enter" }, keys, mac)) {
            // Kept from the page even while disabled, so it never accepts an agent request.
            event.preventDefault();
            if (!disabled) latest.current.onSubmit();
          } else if (onCancel && !disabled && chordMatches({ key: "Escape" }, keys, mac)) {
            // A save or post under way can't be discarded from here, as its Cancel can't.
            event.preventDefault();
            onCancel();
          }
        }}
        className={cn(SIZES[size], "overflow-y-auto px-3 py-2 text-ui break-words")}
      >
        {/* The panel primitive takes no ref, so Preview's focus lands here; keys bubble up. */}
        <div ref={previewRef} tabIndex={-1} className="outline-none">
          {text.trim() ? preview : <p className="text-muted-foreground">Nothing to preview yet.</p>}
        </div>
      </TabsContent>
      <div className="flex min-h-8 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-t border-border px-3 py-1.5">
        {/* Each hint stays whole, so a narrow composer moves the shortcut to its own line. */}
        <p className="flex min-w-0 flex-1 basis-40 flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
          {(attachments || attachmentsUnavailableReason) && (
            <span>
              {attachments
                ? "Paste or drop images and videos to attach"
                : attachmentsUnavailableReason}
              {/* Kept with the text before it, so a wrapped line never starts with it. */}
              <span aria-hidden> ·</span>
            </span>
          )}
          <span className="whitespace-nowrap">
            {chordKeys({ mod: true, key: "Enter" }, mac).join(mac ? "" : "+")} to {submitVerb}
          </span>
        </p>
        {footerActions && (
          // The actions wrap too, so a very narrow box keeps every button inside it.
          <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-1.5">
            {footerActions}
          </div>
        )}
      </div>
      {children}
      {attachments && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ATTACHMENT_ACCEPT}
          hidden
          data-testid="markdown-composer-file-input"
          onChange={(event) => {
            attach(Array.from(event.target.files ?? []));
            event.target.value = "";
            textareaRef.current?.focus();
          }}
        />
      )}
    </Tabs>
  );
}

/** The reference's nine formatting buttons; mouse presses leave the selection in the textarea. */
function MarkdownToolbar({
  disabled,
  onFormat,
}: {
  disabled: boolean;
  onFormat: (format: MarkdownFormat) => void;
}) {
  return (
    <div role="toolbar" aria-label="Formatting" className="flex flex-wrap items-center gap-px">
      {TOOLBAR_FORMATS.map((command) => (
        <Tooltip key={command.format}>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={command.label}
              disabled={disabled}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onFormat(command.format)}
              className="text-muted-foreground hover:text-foreground"
            >
              <MarkdownToolIcon format={command.format} />
            </Button>
          </TooltipTrigger>
          <TooltipContent shortcut={command.chord ? chordKeys(command.chord) : undefined}>
            {command.label}
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}
