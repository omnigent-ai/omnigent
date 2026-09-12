import {
  forwardRef,
  useLayoutEffect,
  useRef,
  type ComponentPropsWithRef,
  type ComponentPropsWithoutRef,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { ArrowUpIcon, Loader2Icon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { isComposerSendKey } from "@/lib/composerSendShortcutPreferences";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";

export const COMPOSER_COLUMN_WIDTH = `w-full ${CHAT_COLUMN_WIDTH}`;

/**
 * Minimum free space (px) the action row keeps between its leading and
 * trailing groups. Once the row is narrower than both groups plus this gap,
 * the controls' text labels collapse to icons instead of wrapping.
 */
export const COMPOSER_LABELS_MIN_GAP_PX = 24;

/** Hides a control's text label while the action row is collapsed to icons. */
export const COMPOSER_COLLAPSED_LABEL_CLASS =
  "group-data-[labels=collapsed]/composer-actions:hidden";

export interface ComposerKeyIntent {
  shouldSubmitFromKeyboard: boolean;
  shouldPreferSendOverCompletion: boolean;
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
  };
  slots?: {
    beforeInput?: ReactNode;
    inputPrefix?: ReactNode;
    inputBackdrop?: ReactNode;
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
        "composer-reference-surface relative flex w-full flex-col rounded-2xl border transition-shadow duration-150 has-[textarea:focus]:shadow-[var(--composer-shadow-focus)]",
        className,
      )}
      {...props}
    >
      {slots?.beforeInput}
      <ComposerInputArea
        className={slots?.inputPrefix ? "max-h-[320px] overflow-y-auto" : undefined}
      >
        {slots?.inputPrefix}
        {slots?.inputBackdrop}
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

export function ComposerTextInput({
  input,
  keyboard,
}: Pick<ChatComposerProps, "input" | "keyboard">) {
  return (
    <ComposerTextarea
      {...input}
      onKeyDown={(event) => {
        if (keyboard.preventsKeyboardSubmit && event.key === "Enter") return;
        const shouldSubmitFromKeyboard = isComposerSendKey(
          { ...event, isComposing: event.nativeEvent.isComposing },
          keyboard.submitWithModEnter,
          keyboard.preventsKeyboardSubmit,
        );
        input.onKeyDown?.(event, {
          shouldSubmitFromKeyboard,
          shouldPreferSendOverCompletion: keyboard.submitWithModEnter && shouldSubmitFromKeyboard,
        });
      }}
    />
  );
}

export function ComposerInputArea({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div className={cn("relative overflow-hidden px-3 pt-3 pb-1 text-ui", className)} {...props} />
  );
}

export const ComposerTextarea = forwardRef<
  HTMLTextAreaElement,
  ComponentPropsWithoutRef<"textarea">
>(function ComposerTextarea(
  { className, onKeyDown, onCompositionStart, onCompositionEnd, ...props },
  ref,
) {
  const isComposingRef = useRef(false);
  return (
    <textarea
      ref={ref}
      className={cn(
        "relative max-h-[180px] w-full resize-none overflow-y-auto border-none bg-transparent p-0 text-ui text-foreground outline-none [scrollbar-width:none] placeholder:text-muted-foreground disabled:opacity-60 md:select-text [&::-webkit-scrollbar]:hidden",
        className,
      )}
      {...props}
      onCompositionStart={(event) => {
        isComposingRef.current = true;
        onCompositionStart?.(event);
      }}
      onCompositionEnd={(event) => {
        isComposingRef.current = false;
        onCompositionEnd?.(event);
      }}
      onKeyDown={(event) => {
        if (!isImeCompositionKeyEvent(event, isComposingRef.current)) onKeyDown?.(event);
      }}
    />
  );
});

export const ComposerActionRow = forwardRef<HTMLDivElement, ComponentPropsWithoutRef<"div">>(
  function ComposerActionRow({ className, ...props }, ref) {
    return (
      <div
        ref={ref}
        className={cn(
          "group/composer-actions @container/composer-actions relative flex min-w-0 flex-wrap items-center justify-between gap-2 px-2 pt-1 pb-2",
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
          "bg-foreground hover:opacity-80 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100",
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
