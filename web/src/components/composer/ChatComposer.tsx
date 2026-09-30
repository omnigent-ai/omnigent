import {
  forwardRef,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentPropsWithRef,
  type ComponentPropsWithoutRef,
  type KeyboardEvent,
  type MutableRefObject,
  type ReactNode,
  type RefObject,
} from "react";
import { ArrowUpIcon, Loader2Icon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import {
  markerRevealRanges,
  normalizeLineBreaks,
  plainComposerMarkdown,
  tokenizeComposerMarkdown,
  type ComposerMarkdown,
  type RevealRange,
} from "@/lib/composerMarkdown";
import {
  ComposerCompactView,
  ComposerHighlightLayer,
  type ComposerAccentRange,
} from "./ComposerHighlightLayer";
import { compactLayout } from "@/lib/composerCompact";
import {
  isComposerAltNewlineKey,
  isComposerSendKey,
  isComposerSteerAllKey,
} from "@/lib/composerSendShortcutPreferences";
import { fenceArrowDown, fenceEnter, fenceNewline, type ComposerEdit } from "@/lib/composerEditing";
import {
  NO_PAIRS,
  editAfterKeystroke,
  editBeforeKeystroke,
  followKeystroke,
  isPairChar,
  pairsAround,
  type PairState,
} from "@/lib/composerAutoPair";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";

export const COMPOSER_COLUMN_WIDTH = `w-full ${CHAT_COLUMN_WIDTH}`;

/**
 * The composer layout contract: one 12px inset, two roles.
 *
 * Inside the card, every content row - the input text, chip rows
 * (attachment/mention chips), feedback rows (attachment/command errors), and
 * the action row's controls - aligns to a shared left/right inset line 12px
 * from the card's edges. Outside the card, the docked trays (workspace bar,
 * queued-messages strip, sub-agent tray) nest 12px in from the card's outer
 * edges: a tray is a shelf peeking above the card, not a content row, so it
 * keeps its own inset rather than sharing the card's border box.
 *
 * Padding vs margin follows what each row's border box must coincide with:
 * chip and action rows are measured through their children (chips, buttons),
 * so they pad; a feedback row's own box sits on the inset line, so it uses
 * margins. Vertical rhythm: the input area is `pt-3 pb-1`, each content row
 * carries `pb-2`, and the action row is `pt-1 pb-2`.
 */
export const COMPOSER_CONTENT_INSET_CLASS = "px-3";
export const COMPOSER_BLOCK_INSET_CLASS = "mx-3";
export const COMPOSER_TRAY_INSET_CLASS = "mx-3";

/**
 * Minimum free space (px) the action row keeps between its leading and
 * trailing groups. Once the row is narrower than both groups plus this gap,
 * the controls' text labels collapse to icons instead of wrapping.
 */
export const COMPOSER_LABELS_MIN_GAP_PX = 24;

/** Hides a control's text label while the action row is collapsed to icons. */
export const COMPOSER_COLLAPSED_LABEL_CLASS =
  "group-data-[labels=collapsed]/composer-actions:hidden";

/**
 * Hides a workspace-bar chip's text label while the bar is collapsed to icons.
 * Only the directory and branch chips carry it: the PR number and the context
 * percentage are short and informative, so they stay visible.
 */
export const COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS =
  "group-data-[labels=collapsed]/composer-workspace:hidden";

export interface ComposerKeyIntent {
  shouldSubmitFromKeyboard: boolean;
  shouldPreferSendOverCompletion: boolean;
  shouldSteerAllFromKeyboard: boolean;
}

interface ChatComposerProps extends Omit<ComponentPropsWithoutRef<"div">, "children"> {
  keyboard: {
    submitWithModEnter: boolean;
    preventsKeyboardSubmit: boolean;
  };
  input: Omit<ComponentPropsWithRef<"textarea">, "onKeyDown"> & {
    onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>, intent: ComposerKeyIntent) => void;
    "data-testid"?: string;
    "data-slash-command"?: string;
    "data-has-draft"?: string;
    accentRange?: ComposerAccentRange | null;
    onCompactChange?: () => void;
  };
  slots?: {
    beforeInput?: ReactNode;
    inputPrefix?: ReactNode;
    inputHint?: ReactNode;
    attachments?: ReactNode;
  };
  actions: {
    leading: ReactNode;
    trailing: ReactNode;
    testId?: string;
    leadingTestId?: string;
    trailingTestId?: string;
  };
}

export const ChatComposer = forwardRef<HTMLDivElement, ChatComposerProps>(function ChatComposer(
  { className, input, keyboard, slots, actions, ...props },
  ref,
) {
  const actionRowRef = useRef<HTMLDivElement>(null);
  const actionWidthRef = useRef<HTMLDivElement>(null);
  const leadingRef = useRef<HTMLDivElement>(null);
  const trailingRef = useRef<HTMLDivElement>(null);
  useCollapsedComposerLabels(actionRowRef, actionWidthRef, leadingRef, trailingRef);
  return (
    <div
      ref={ref}
      data-composer-card
      className={cn(
        "composer-reference-surface relative flex w-full flex-col rounded-2xl border transition-shadow duration-150 has-[textarea:focus]:shadow-[var(--composer-shadow-focus)] md:min-h-[105px]",
        className,
      )}
      {...props}
    >
      {slots?.beforeInput}
      <ComposerInputArea
        className={slots?.inputPrefix ? "max-h-[320px] overflow-y-auto" : undefined}
      >
        {slots?.inputPrefix}
        <ComposerTextInput input={input} keyboard={keyboard} />
        {slots?.inputHint}
      </ComposerInputArea>
      {slots?.attachments}
      <ComposerActionRow ref={actionRowRef} data-testid={actions.testId}>
        {/* Zero-height width probe: resize-observed instead of the row itself,
            whose height the collapse verdict can change. */}
        <div ref={actionWidthRef} className="absolute inset-x-0 top-0 h-0" />
        <ComposerActionGroup ref={leadingRef} side="left" data-testid={actions.leadingTestId}>
          {actions.leading}
        </ComposerActionGroup>
        <ComposerActionGroup ref={trailingRef} side="right" data-testid={actions.trailingTestId}>
          {actions.trailing}
        </ComposerActionGroup>
      </ComposerActionRow>
    </div>
  );
});

/**
 * Collapse the action row's text labels to icons whenever its leading and
 * trailing groups would not fit on one line with `COMPOSER_LABELS_MIN_GAP_PX`
 * between them, and restore them as soon as they fit again. The verdict lands
 * on the row as `data-labels="collapsed"`, which `COMPOSER_COLLAPSED_LABEL_CLASS`
 * turns into `display: none` on each label.
 *
 * Every measurement probes the expanded layout: the attribute is removed, the
 * groups' natural widths are read, and the verdict is written back within the
 * same task, so the probe never paints and the verdict never depends on the
 * previous one. Re-measured when the row's width changes and when the controls
 * inside it change.
 */
function useCollapsedComposerLabels(
  rowRef: RefObject<HTMLDivElement | null>,
  widthRef: RefObject<HTMLDivElement | null>,
  leadingRef: RefObject<HTMLDivElement | null>,
  trailingRef: RefObject<HTMLDivElement | null>,
) {
  useLayoutEffect(() => {
    const row = rowRef.current;
    const width = widthRef.current;
    const leading = leadingRef.current;
    const trailing = trailingRef.current;
    if (!row || !width || !leading || !trailing) return;
    const measure = () => {
      const style = getComputedStyle(row);
      const available =
        row.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      // Not laid out yet (hidden, or jsdom): keep the current verdict.
      if (!(available > 0)) return;
      delete row.dataset.labels;
      const gap = Math.max(parseFloat(style.columnGap) || 0, COMPOSER_LABELS_MIN_GAP_PX);
      if (leading.scrollWidth + trailing.scrollWidth + gap > available)
        row.dataset.labels = "collapsed";
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(width);
    const mutationObserver = new MutationObserver(measure);
    mutationObserver.observe(row, { childList: true, characterData: true, subtree: true });
    return () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [rowRef, widthRef, leadingRef, trailingRef]);
}

/**
 * Collapse the workspace bar's directory and branch labels to icons whenever
 * the bar cannot show every label in full — a label is truncating (the PR
 * number included, since freeing the directory and branch text gives it room),
 * or the row overflows its width — and restore them once they fit again. The
 * verdict lands on the bar as `data-labels="collapsed"`, which
 * `COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS` turns into `display: none` on the
 * labels that carry it.
 *
 * The bar's height is fixed, so it is safe to resize-observe directly — the
 * collapse never changes the observed box, so there is no probe element and no
 * observer loop. That holds only while the bar is mounted in a width-constrained
 * parent (it is, in both composers); a shrink-to-fit parent would let the
 * collapse change the bar's width and re-fire the observer. Every measure probes
 * the expanded layout first (labels shown), so the verdict never feeds on its
 * own collapsed widths.
 */
export function useCollapsedWorkspaceLabels(barRef: RefObject<HTMLElement | null>) {
  useLayoutEffect(() => {
    const bar = barRef.current;
    if (!bar) return;
    const measure = () => {
      delete bar.dataset.labels;
      // Not laid out yet (hidden, or jsdom): keep the current verdict.
      if (!(bar.clientWidth > 0)) return;
      const labels = bar.querySelectorAll<HTMLElement>("[data-workspace-collapse-label]");
      const cramped =
        bar.scrollWidth > bar.clientWidth + 1 ||
        Array.from(labels).some((label) => label.scrollWidth > label.clientWidth + 1);
      if (cramped) bar.dataset.labels = "collapsed";
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(bar);
    const mutationObserver = new MutationObserver(measure);
    mutationObserver.observe(bar, { childList: true, characterData: true, subtree: true });
    return () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [barRef]);
}

/**
 * Insert a line break at the caret as typed input, so onChange sees it. The
 * ``insertText`` command also keeps it on the undo stack; the fallback covers
 * environments without it.
 */
function insertLineBreak(textarea: HTMLTextAreaElement) {
  const inserted =
    typeof document.execCommand === "function" && document.execCommand("insertText", false, "\n");
  if (inserted) return;
  textarea.setRangeText("\n", textarea.selectionStart, textarea.selectionEnd, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

export function ComposerTextInput({
  input,
  keyboard,
}: Pick<ChatComposerProps, "input" | "keyboard">) {
  const parsedRef = useRef<ComposerParse | null>(null);
  return (
    <ComposerTextarea
      {...input}
      parsedRef={parsedRef}
      onKeyDown={(event) => {
        const textarea = event.currentTarget;
        // Keys reuse the draft's current parse; one that's stale or missing
        // (a draft past the parse cap) skips code-block handling, never
        // re-tokenizes.
        const parse = parsedRef.current;
        const markdown = parse?.text === textarea.value ? parse.markdown : null;
        // A newline in every mode and on touch devices; completion menus never
        // see it. In a quoted or listed code block it keeps the block going.
        if (isComposerAltNewlineKey({ ...event, isComposing: event.nativeEvent.isComposing })) {
          event.preventDefault();
          const { selectionStart, selectionEnd } = textarea;
          const continued =
            markdown && fenceNewline(textarea.value, selectionStart, selectionEnd, markdown);
          if (continued) applyComposerEdit(textarea, continued);
          else insertLineBreak(textarea);
          return;
        }
        const plain = !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey;
        // Enter inside a fenced block makes a newline, and exits from its
        // empty last line; Cmd/Ctrl+Enter and the send button still send.
        const inFence =
          markdown && event.key === "Enter" && plain && !event.nativeEvent.isComposing
            ? fenceEnter(textarea.value, textarea.selectionStart, textarea.selectionEnd, markdown)
            : null;
        const editFence = () => {
          if (inFence?.kind !== "edit" || event.defaultPrevented) return;
          event.preventDefault();
          applyComposerEdit(textarea, inFence.edit);
        };
        if (keyboard.preventsKeyboardSubmit && event.key === "Enter") {
          editFence();
          return;
        }
        const shouldSubmitFromKeyboard =
          inFence === null &&
          isComposerSendKey(
            { ...event, isComposing: event.nativeEvent.isComposing },
            keyboard.submitWithModEnter,
            keyboard.preventsKeyboardSubmit,
          );
        const shouldSteerAllFromKeyboard = isComposerSteerAllKey(
          { ...event, isComposing: event.nativeEvent.isComposing },
          keyboard.submitWithModEnter,
          keyboard.preventsKeyboardSubmit,
        );
        // Menus (slash commands, mentions) see the key first and may claim it.
        input.onKeyDown?.(event, {
          shouldSubmitFromKeyboard,
          shouldPreferSendOverCompletion: keyboard.submitWithModEnter && shouldSubmitFromKeyboard,
          shouldSteerAllFromKeyboard,
        });
        editFence();
        if (markdown && event.key === "ArrowDown" && plain && !event.defaultPrevented) {
          const down = fenceArrowDown(
            textarea.value,
            textarea.selectionStart,
            textarea.selectionEnd,
            markdown,
          );
          if (!down) return;
          event.preventDefault();
          if ("edit" in down) applyComposerEdit(textarea, down.edit);
          else textarea.setSelectionRange(down.move, down.move);
        }
      }}
    />
  );
}

/**
 * Apply an edit the composer makes for the user as one undoable change: the
 * browser's own insertText command keeps it on the textarea's undo stack.
 * Where that command isn't available the value is set directly (not undoable).
 */
export function applyComposerEdit(textarea: HTMLTextAreaElement, edit: ComposerEdit) {
  textarea.focus();
  textarea.setSelectionRange(edit.start, edit.end);
  let applied: boolean;
  try {
    applied =
      typeof document.execCommand === "function" &&
      document.execCommand("insertText", false, edit.insert);
  } catch {
    applied = false;
  }
  if (!applied) {
    const value =
      textarea.value.slice(0, edit.start) + edit.insert + textarea.value.slice(edit.end);
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
      textarea,
      value,
    );
    textarea.dispatchEvent(
      new InputEvent("input", { bubbles: true, inputType: "insertReplacementText" }),
    );
  }
  textarea.setSelectionRange(edit.caret, edit.caret);
}

export function ComposerInputArea({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn(
        "composer-input-text relative overflow-hidden pt-3 pb-1 text-ui",
        COMPOSER_CONTENT_INSET_CLASS,
        className,
      )}
      {...props}
    />
  );
}

/**
 * One wrapping row of chips (attachment tiles, mention chips) on the shared
 * inset line. Chips are measured through their own boxes, so the row pads
 * rather than margins; the chip type picks its gap via ``className``.
 */
export function ComposerChipRow({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start gap-2 pb-2",
        COMPOSER_CONTENT_INSET_CLASS,
        className,
      )}
      {...props}
    />
  );
}

/**
 * One feedback line under the input (rejected attachments, slash-command
 * errors and /help output). The row's own border box sits on the shared
 * inset line, so it margins rather than pads.
 */
export function ComposerFeedbackRow({
  tone = "muted",
  className,
  ...props
}: ComponentPropsWithoutRef<"div"> & { tone?: "muted" | "error" }) {
  return (
    <div
      className={cn(
        "pb-2 text-sm whitespace-pre-wrap",
        COMPOSER_BLOCK_INSET_CLASS,
        tone === "error" ? "text-destructive" : "text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A safety cap far above a several-thousand-line paste: past this many
 * characters Markdown is not parsed, though a command token is still tinted.
 */
export const MAX_HIGHLIGHTED_DRAFT_LENGTH = 1_000_000;

/** The draft's current parse, and the text it's of. */
export interface ComposerParse {
  text: string;
  /** Null when the draft isn't parsed (past the parse cap). */
  markdown: ComposerMarkdown | null;
}

type ComposerTextareaProps = ComponentPropsWithoutRef<"textarea"> & {
  /** Receives the draft's current parse after each render, for key handling. */
  parsedRef?: MutableRefObject<ComposerParse | null>;
  /** Draft range to tint as a slash command or skill token. */
  accentRange?: ComposerAccentRange | null;
  /**
   * Called in the same task as the switch to or from the compact preview,
   * whose height differs, so a transcript can re-pin before the next paint.
   */
  onCompactChange?: () => void;
};

/**
 * The draft input. Markdown in the draft is styled live by an aria-hidden
 * highlight layer painted behind the textarea: while the layer is shown, the
 * textarea's glyphs are transparent but it keeps the caret, selection, IME and
 * every input behavior. The layer mirrors the textarea's box and typography so
 * each glyph lands exactly under the one it replaces, and follows its height
 * and scroll. It steps aside during IME composition so the native composition
 * text and underline show. Markdown markers are hidden except those of the
 * tokens the focused textarea's caret or selection touches; blur hides them all.
 *
 * Hidden markers keep their width while editing, since the caret must line up.
 * Once the textarea has lost focus (and no press is still in progress), a
 * compact preview takes its place: markers take no space and code fences
 * collapse. The textarea stays mounted, focusable and in the accessibility
 * tree, just out of the flow and not painted; a click on the preview focuses
 * it with the caret on the character clicked, and keyboard focus keeps its
 * previous selection.
 */
export const ComposerTextarea = forwardRef<HTMLTextAreaElement, ComposerTextareaProps>(
  function ComposerTextarea(
    {
      className,
      onKeyDown,
      onCompositionStart,
      onCompositionEnd,
      onScroll,
      onFocus,
      onBlur,
      onSelect,
      onInput,
      onKeyUp,
      onMouseUp,
      accentRange,
      parsedRef,
      onCompactChange,
      value,
      ...props
    },
    ref,
  ) {
    const isComposingRef = useRef(false);
    // Set once the textarea has settled out of focus; the preview needs it.
    const [unfocused, setUnfocused] = useState(false);
    const [composing, setComposing] = useState(false);
    const [selection, setSelection] = useState<{ start: number; end: number } | null>(null);
    const textareaRef = useRef<HTMLTextAreaElement | null>(null);
    const layerRef = useRef<HTMLDivElement>(null);
    const setTextareaRef = useCallback(
      (node: HTMLTextAreaElement | null) => {
        textareaRef.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) ref.current = node;
      },
      [ref],
    );
    // The textarea shows every line break as LF; the layer lays out that text.
    const shown = useMemo(
      () => (typeof value === "string" ? normalizeLineBreaks(value) : null),
      [value],
    );
    const accent = useMemo(() => {
      if (!shown || !accentRange || accentRange.end <= accentRange.start) return null;
      return { start: shown.offset(accentRange.start), end: shown.offset(accentRange.end) };
    }, [shown, accentRange]);
    const parsed = shown !== null && shown.text.length <= MAX_HIGHLIGHTED_DRAFT_LENGTH;
    const needsPlain = !parsed && accent !== null;
    const markdown = useMemo(() => {
      if (!shown) return null;
      if (parsed) return tokenizeComposerMarkdown(shown.text);
      return needsPlain ? plainComposerMarkdown(shown.text) : null;
    }, [shown, parsed, needsPlain]);
    const highlighted = !composing && markdown !== null && (markdown.styled || accent !== null);
    const revealRanges = useStableRanges(
      useMemo(
        () =>
          parsed && markdown && selection
            ? markerRevealRanges(markdown, selection.start, selection.end)
            : NO_RANGES,
        [parsed, markdown, selection],
      ),
    );
    // Composition text is not the draft yet; its caret is read once it commits.
    const readSelection = useCallback(() => {
      const textarea = textareaRef.current;
      if (!textarea || isComposingRef.current) return;
      if (textarea.ownerDocument.activeElement !== textarea) {
        setSelection(null);
        return;
      }
      const { selectionStart: start, selectionEnd: end } = textarea;
      setSelection((previous) =>
        previous?.start === start && previous.end === end ? previous : { start, end },
      );
    }, []);
    useEffect(() => {
      if (!highlighted) return;
      // Arrow keys, drag selection and programmatic moves all fire this.
      document.addEventListener("selectionchange", readSelection);
      return () => document.removeEventListener("selectionchange", readSelection);
    }, [highlighted, readSelection]);
    useTypingEdits(textareaRef, isComposingRef);
    useHighlightLayerSync(textareaRef, layerRef, highlighted, shown?.text);
    const scheduleUnfocused = useSettledBlur(textareaRef, setUnfocused);
    const compact = useMemo(
      () => (unfocused && highlighted && parsed && markdown ? compactLayout(markdown) : null),
      [unfocused, highlighted, parsed, markdown],
    );
    const compacted = compact?.collapsed === true;
    const onCompactChangeRef = useRef(onCompactChange);
    useLayoutEffect(() => {
      onCompactChangeRef.current = onCompactChange;
    });
    const wasCompactedRef = useRef(compacted);
    useLayoutEffect(() => {
      if (wasCompactedRef.current === compacted) return;
      wasCompactedRef.current = compacted;
      onCompactChangeRef.current?.();
    }, [compacted]);
    useOutOfFlow(textareaRef, compacted);
    const placeCaret = useCallback((at: number) => {
      const textarea = textareaRef.current;
      if (!textarea || textarea.disabled) return;
      textarea.focus({ preventScroll: true });
      textarea.setSelectionRange(at, at);
    }, []);
    useLayoutEffect(() => {
      if (parsedRef)
        parsedRef.current = { text: shown?.text ?? "", markdown: parsed ? markdown : null };
    });
    return (
      <div
        className="composer-input-text relative text-ui"
        data-compact={compacted ? "" : undefined}
        // The textarea below the preview adds no height or scroll (nor its
        // line box's strut); a quote's bar still reaches into the left gutter.
        // Inline styles here and below: a class change on these restyles
        // every span of the draft.
        style={compacted ? { overflowY: "clip", lineHeight: 0 } : undefined}
      >
        {highlighted && markdown && (
          <ComposerHighlightLayer
            ref={layerRef}
            markdown={markdown}
            accentRange={accent}
            revealRanges={revealRanges}
            disabled={props.disabled}
            hidden={compacted}
          />
        )}
        {compact && compacted && (
          <ComposerCompactView
            layout={compact}
            accentRange={accent}
            disabled={props.disabled}
            textareaRef={textareaRef}
            onPlaceCaret={placeCaret}
          />
        )}
        <textarea
          ref={setTextareaRef}
          value={value}
          className={cn(
            "composer-input-text relative max-h-[180px] w-full resize-none overflow-y-auto border-none bg-transparent p-0 text-ui text-foreground outline-none [scrollbar-width:none] placeholder:text-muted-foreground disabled:opacity-60 md:min-h-[42px] md:select-text [&::-webkit-scrollbar]:hidden",
            // The layer paints the glyphs; selected text must not repaint over it.
            highlighted &&
              "text-transparent caret-foreground [-webkit-text-fill-color:transparent] selection:text-transparent selection:[-webkit-text-fill-color:transparent]",
            className,
          )}
          {...props}
          // Under the preview, unpainted but focusable. A negative margin (see
          // useOutOfFlow) keeps its box and scroll; repositioning it would re-lay
          // out all its text.
          style={
            compacted
              ? { ...props.style, opacity: 0, pointerEvents: "none", verticalAlign: "top" }
              : props.style
          }
          onScroll={(event) => {
            if (layerRef.current) layerRef.current.scrollTop = event.currentTarget.scrollTop;
            onScroll?.(event);
          }}
          onFocus={(event) => {
            setUnfocused(false);
            readSelection();
            onFocus?.(event);
          }}
          onBlur={(event) => {
            setSelection(null);
            scheduleUnfocused();
            onBlur?.(event);
          }}
          onSelect={(event) => {
            readSelection();
            onSelect?.(event);
          }}
          onInput={(event) => {
            readSelection();
            onInput?.(event);
          }}
          onKeyUp={(event) => {
            readSelection();
            onKeyUp?.(event);
          }}
          onMouseUp={(event) => {
            readSelection();
            onMouseUp?.(event);
          }}
          onCompositionStart={(event) => {
            isComposingRef.current = true;
            setComposing(true);
            onCompositionStart?.(event);
          }}
          onCompositionEnd={(event) => {
            isComposingRef.current = false;
            setComposing(false);
            readSelection();
            onCompositionEnd?.(event);
          }}
          onKeyDown={(event) => {
            if (!isImeCompositionKeyEvent(event, isComposingRef.current)) onKeyDown?.(event);
          }}
        />
      </div>
    );
  },
);

const NO_RANGES: readonly RevealRange[] = [];

/**
 * While `active`, the textarea takes no height in the flow: a negative bottom
 * margin as tall as it is, kept current as it auto-grows (dictation can add
 * text while the preview shows).
 */
function useOutOfFlow(textareaRef: RefObject<HTMLTextAreaElement | null>, active: boolean) {
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!active || !textarea) return;
    const sync = () => {
      textarea.style.marginBottom = `-${textarea.getBoundingClientRect().height}px`;
    };
    sync();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(sync);
    observer?.observe(textarea);
    return () => {
      observer?.disconnect();
      textarea.style.marginBottom = "";
    };
  }, [textareaRef, active]);
}

/**
 * Report the textarea as unfocused once focus has settled elsewhere: after a
 * press that blurred it ends (so a click on a control lands before the
 * composer changes height), and not when focus comes straight back or the
 * window alone lost it. A textarea that starts unfocused is checked shortly
 * after mount, once the page has had its chance to focus it. Returns the
 * function to call on blur.
 */
function useSettledBlur(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  setUnfocused: (unfocused: boolean) => void,
): () => void {
  const pressedRef = useRef(false);
  const pendingRef = useRef<() => void>(() => {});
  const check = useCallback(() => {
    const textarea = textareaRef.current;
    if (textarea && textarea.ownerDocument.activeElement !== textarea) setUnfocused(true);
  }, [textareaRef, setUnfocused]);
  useEffect(() => {
    const press = () => {
      pressedRef.current = true;
    };
    const release = () => {
      pressedRef.current = false;
    };
    document.addEventListener("pointerdown", press, true);
    document.addEventListener("pointerup", release, true);
    document.addEventListener("pointercancel", release, true);
    const mount = window.setTimeout(check, 50);
    return () => {
      document.removeEventListener("pointerdown", press, true);
      document.removeEventListener("pointerup", release, true);
      document.removeEventListener("pointercancel", release, true);
      window.clearTimeout(mount);
      pendingRef.current();
    };
  }, [check]);
  return useCallback(() => {
    pendingRef.current();
    let timer = 0;
    const later = () => {
      timer = window.setTimeout(check, 0);
    };
    pendingRef.current = () => {
      document.removeEventListener("pointerup", later, true);
      document.removeEventListener("pointercancel", later, true);
      window.clearTimeout(timer);
    };
    if (!pressedRef.current) {
      later();
      return;
    }
    // After the press ends and its click has run.
    document.addEventListener("pointerup", later, { capture: true, once: true });
    document.addEventListener("pointercancel", later, { capture: true, once: true });
  }, [check]);
}

/** Keep the previous array while the ranges are unchanged, so the layer skips re-rendering. */
function useStableRanges(ranges: readonly RevealRange[]): readonly RevealRange[] {
  const previousRef = useRef(ranges);
  const previous = previousRef.current;
  const same =
    previous.length === ranges.length &&
    previous.every((range, i) => range.start === ranges[i].start && range.end === ranges[i].end);
  if (!same) previousRef.current = ranges;
  return same ? previous : ranges;
}

/**
 * Edits made as the user types Markdown: an opening `` ` ``, `*` or `_` gets
 * its closer (see `composerAutoPair`), a closing marker tidies a stray space
 * inside its pair or completes an open `**`, and a third backtick on an empty
 * line makes a code block. Only a typed character triggers one (an
 * `insertText` input, not composing): never a paste, IME composition,
 * dictation, history recall or draft restore, which don't arrive as typed
 * input. Each is its own undoable change, applied after the keystroke lands,
 * so one undo restores exactly what was typed. Backspace in an empty inserted
 * pair and a marker typed over a selection replace the keystroke instead.
 */
function useTypingEdits(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  isComposingRef: RefObject<boolean>,
) {
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    let applying = false;
    // The closers inserted here, valid while the draft is `trackedValue`.
    let state: PairState = NO_PAIRS;
    let trackedValue = textarea.value;
    // The draft and selection before the next keystroke: refreshed after every
    // change, and at `beforeinput` when that fires (not for execCommand).
    let before = { value: textarea.value, start: 0, end: 0 };
    const snapshot = () => {
      const { value, selectionStart, selectionEnd } = textarea;
      // A change that didn't come as input (restore, dictation) ends tracking.
      if (value !== trackedValue) state = NO_PAIRS;
      trackedValue = value;
      before = { value, start: selectionStart, end: selectionEnd };
    };
    const apply = (edit: ComposerEdit) => {
      applying = true;
      try {
        applyComposerEdit(textarea, edit);
      } finally {
        applying = false;
      }
      trackedValue = textarea.value;
      snapshot();
    };
    const onBeforeInput = (event: Event) => {
      const input = event as InputEvent;
      if (applying || input.isComposing || isComposingRef.current) return;
      snapshot();
      const { value, selectionStart, selectionEnd } = textarea;
      const replaced = editBeforeKeystroke(
        value,
        selectionStart,
        selectionEnd,
        input.inputType,
        input.data,
        state,
      );
      if (!replaced) return;
      input.preventDefault();
      if (replaced.edit) apply(replaced.edit);
      if (replaced.selection) textarea.setSelectionRange(...replaced.selection);
      state = replaced.state;
      snapshot();
    };
    const onInput = (event: Event) => {
      const input = event as InputEvent;
      if (applying) return;
      const { value, selectionStart } = textarea;
      state = followKeystroke(state, input.inputType, before, value, selectionStart);
      trackedValue = value;
      snapshot();
      const typed = input.data;
      if (input.inputType !== "insertText" || input.isComposing || isComposingRef.current) return;
      if (typed?.length !== 1 || (!isPairChar(typed) && typed !== " ")) {
        state = { ...state, swallow: null };
        return;
      }
      queueMicrotask(() => {
        const { value: text, selectionStart: caret, selectionEnd } = textarea;
        if (caret !== selectionEnd || text[caret - 1] !== typed) return;
        const result = editAfterKeystroke(text, caret, typed, state);
        state = result.state;
        if (result.edit) apply(result.edit);
      });
    };
    // A caret moved out of a pair's text leaves its closer as plain text.
    const onSelection = () => {
      if (document.activeElement !== textarea || applying) return;
      const { selectionStart, selectionEnd } = textarea;
      state = {
        pairs: selectionStart === selectionEnd ? pairsAround(state.pairs, selectionStart) : [],
        swallow: state.swallow?.at === selectionStart ? state.swallow : null,
      };
      snapshot();
    };
    textarea.addEventListener("beforeinput", onBeforeInput);
    textarea.addEventListener("input", onInput);
    document.addEventListener("selectionchange", onSelection);
    return () => {
      textarea.removeEventListener("beforeinput", onBeforeInput);
      textarea.removeEventListener("input", onInput);
      document.removeEventListener("selectionchange", onSelection);
    };
  }, [textareaRef, isComposingRef]);
}

/**
 * Keep the highlight layer on the textarea's box: its height follows the
 * textarea's (auto-grow, the max-height cap) and its scroll offset is copied on
 * mount, resize, scroll and the frame after each edit. That frame's layout is
 * already clean, so the copy never forces an extra layout per keystroke.
 */
function useHighlightLayerSync(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  layerRef: RefObject<HTMLDivElement | null>,
  highlighted: boolean,
  text: string | undefined,
) {
  useEffect(() => {
    if (!highlighted) return;
    const frame = requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      const layer = layerRef.current;
      if (textarea && layer && layer.scrollTop !== textarea.scrollTop)
        layer.scrollTop = textarea.scrollTop;
    });
    return () => cancelAnimationFrame(frame);
  }, [textareaRef, layerRef, highlighted, text]);
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    const layer = layerRef.current;
    if (!highlighted || !textarea || !layer) return;
    const sync = () => {
      // The used height, fractions included (offsetHeight rounds). Not laid
      // out yet (hidden, or jsdom): keep the CSS full-height fallback.
      const height = parseFloat(getComputedStyle(textarea).height);
      if (height > 0) layer.style.height = `${height}px`;
      layer.scrollTop = textarea.scrollTop;
    };
    sync();
    if (typeof ResizeObserver === "undefined") return;
    const resizeObserver = new ResizeObserver(sync);
    resizeObserver.observe(textarea);
    return () => resizeObserver.disconnect();
  }, [textareaRef, layerRef, highlighted]);
}

export const ComposerActionRow = forwardRef<HTMLDivElement, ComponentPropsWithoutRef<"div">>(
  function ComposerActionRow({ className, ...props }, ref) {
    return (
      <div
        ref={ref}
        className={cn(
          "group/composer-actions @container/composer-actions relative flex min-w-0 flex-nowrap items-center justify-between gap-2 pt-1 pb-2",
          COMPOSER_CONTENT_INSET_CLASS,
          className,
        )}
        {...props}
      />
    );
  },
);

export const ComposerActionGroup = forwardRef<
  HTMLDivElement,
  ComponentPropsWithoutRef<"div"> & { side: "left" | "right" }
>(function ComposerActionGroup({ side, className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        "flex min-w-0 items-center gap-1",
        side === "left" ? "flex-none overflow-visible" : "ml-auto max-w-full shrink-0",
        className,
      )}
      {...props}
    />
  );
});

export const ComposerSendButton = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "children"> & {
    label: string;
    busy?: boolean;
    interrupt?: boolean;
  }
>(function ComposerSendButton(
  { label, busy = false, interrupt = false, className, ...props },
  ref,
) {
  return (
    <Button
      ref={ref}
      type="submit"
      size="icon"
      variant={interrupt ? "destructive" : "default"}
      className={cn(
        "size-8 shrink-0 rounded-lg transition-opacity md:size-7",
        !interrupt &&
          "hover:opacity-80 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100",
        className,
      )}
      aria-label={label}
      aria-busy={busy}
      {...props}
    >
      {busy ? (
        <Loader2Icon className="size-4 animate-spin" />
      ) : interrupt ? (
        <SquareIcon className="size-4 fill-current" />
      ) : (
        <ArrowUpIcon className="size-4" viewBox="4 4 16 16" />
      )}
      <span className="sr-only">{label}</span>
    </Button>
  );
});
