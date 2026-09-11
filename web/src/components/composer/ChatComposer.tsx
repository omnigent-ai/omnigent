import {
  forwardRef,
  useRef,
  type ComponentPropsWithRef,
  type ComponentPropsWithoutRef,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ArrowUpIcon, Loader2Icon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { isComposerSendKey } from "@/lib/composerSendShortcutPreferences";

export const COMPOSER_COLUMN_WIDTH = "w-full max-w-[720px]";

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
  return (
    <div
      ref={ref}
      data-composer-card
      className={cn(
        "composer-reference-surface relative flex min-h-[105px] w-full flex-col rounded-2xl border transition-shadow duration-150 has-[textarea:focus]:shadow-[var(--composer-shadow-focus)]",
        className,
      )}
      {...props}
    >
      {slots?.beforeInput}
      <ComposerInputArea>
        {slots?.inputBackdrop}
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
              shouldPreferSendOverCompletion:
                keyboard.submitWithModEnter && shouldSubmitFromKeyboard,
            });
          }}
        />
        {slots?.inputHint}
      </ComposerInputArea>
      {slots?.attachments}
      <ComposerActionRow data-testid={actions.testId}>
        <ComposerActionGroup side="left" data-testid={actions.leadingTestId}>
          {actions.leading}
        </ComposerActionGroup>
        <ComposerActionGroup side="right" data-testid={actions.trailingTestId}>
          {actions.trailing}
        </ComposerActionGroup>
      </ComposerActionRow>
    </div>
  );
});

export function ComposerInputArea({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn(
        "relative overflow-hidden px-3 pt-3 pb-1 text-[13px] leading-[20.8px]",
        className,
      )}
      {...props}
    />
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
        "relative min-h-[42px] max-h-[180px] w-full resize-none overflow-y-auto border-none bg-transparent p-0 text-[13px] leading-[20.8px] text-foreground outline-none [scrollbar-width:none] placeholder:text-muted-foreground disabled:opacity-60 md:select-text [&::-webkit-scrollbar]:hidden",
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

export function ComposerActionRow({ className, ...props }: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn(
        "@container/composer-actions flex min-w-0 items-center justify-between gap-2 px-2 pt-1 pb-2",
        className,
      )}
      {...props}
    />
  );
}

export function ComposerActionGroup({
  side,
  className,
  ...props
}: ComponentPropsWithoutRef<"div"> & { side: "left" | "right" }) {
  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-1",
        // The right group shrinks too (its icon buttons are shrink-0), so a
        // long model label truncates under pressure instead of overflowing.
        side === "left" ? "flex-1 overflow-visible" : "min-w-0",
        className,
      )}
      {...props}
    />
  );
}

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
