import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { isBehindOpenLayer } from "@/lib/automaticFocus";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { type AcceptanceToken, createAcceptanceOwnership } from "./acceptanceOwnership";
import type { QuickPickAcceptDetails, QuickPickAcceptResult, QuickPickItem } from "./model";
import { QuickPick, type QuickPickProps } from "./QuickPick";
import { isPromiseLike } from "./useQuickPickSources";

const QUICK_PICK_DIALOG_SELECTOR = "[data-quick-pick-dialog]";
/** Where each open picker returns focus, for a picker that opens in its place. */
const returnFocusTargets = new WeakMap<Element, HTMLElement | null>();

/** A dedicated picker's header: its mode and the shortcut that opens it, such as "Session ⌘↵". */
export interface QuickPickHeading {
  label: string;
  shortcut?: readonly string[];
  /** Trailing content, such as a close button on touch screens. */
  end?: ReactNode;
}

/**
 * What an accept does in a dialog, now or once a promise settles, such as after the row's
 * details load. The dialog stays open while it waits; a cancel, a reopen or a newer accept
 * meanwhile drops the result.
 */
export type QuickPickDialogAcceptResult =
  QuickPickAcceptResult | PromiseLike<QuickPickAcceptResult>;

export interface QuickPickDialogProps<Value> extends Omit<
  QuickPickProps<Value>,
  "onDismiss" | "onClose" | "inputRef" | "autoFocus" | "onAccept" | "onAlternate"
> {
  onAccept: (
    item: QuickPickItem<Value>,
    details: QuickPickAcceptDetails,
  ) => QuickPickDialogAcceptResult;
  /** ⌘↵ for the picker as a whole, settled as an accept is. */
  onAlternate?: () => QuickPickDialogAcceptResult;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Visually hidden dialog title; defaults to `label`. */
  title?: string;
  /** Visually hidden dialog description. */
  description?: string;
  contentClassName?: string;
  /** Classes for the backdrop, such as a clear one so a live preview shows through. */
  overlayClassName?: string;
  /** A header naming the picker's mode, such as a child page's subject. */
  heading?: QuickPickHeading;
  /** Called when the dialog closes without an accept, such as Escape or a click outside. */
  onCancel?: () => void;
}

/**
 * The standard modal QuickPick. Accepting an item closes the dialog unless
 * `onAccept` returns `"keepOpen"`; focus returns to where it was before opening,
 * and then an `afterClose` action that `onAccept` returned runs, free to move it.
 * An accept that returns a promise settles the same way once it resolves, unless a
 * cancel, a reopen or a newer accept came first.
 */
export function QuickPickDialog<Value>({
  open,
  onOpenChange,
  title,
  description,
  contentClassName,
  overlayClassName,
  heading,
  onAccept,
  onAlternate,
  onCancel,
  ...quickPickProps
}: QuickPickDialogProps<Value>) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Radix only restores focus to a DialogTrigger; pickers usually open from shortcuts.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  // The acceptance that owns what's pending: a deferred result, and the close it asked for.
  const [ownership] = useState(createAcceptanceOwnership);
  // Unmounted, pending work is abandoned; a close an accept asked for still finishes, as when
  // the owner renders the dialog only while it's open.
  useEffect(
    () => () => {
      if (!ownership.closing()) ownership.revoke();
    },
    [ownership],
  );
  const latestOnOpenChange = useRef(onOpenChange);
  latestOnOpenChange.current = onOpenChange;
  // Counts the closes asked for, so the commit after each shows whether the owner closed.
  const [closesAsked, setClosesAsked] = useState(0);
  useLayoutEffect(() => {
    // Still open: the owner kept it open, so that close won't come; a later one isn't its.
    if (closesAsked > 0 && open && ownership.closing()) ownership.revoke();
    // Only a new ask re-checks; reopening later is the opening effect's to handle.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [closesAsked]);
  /** Registers this picker's return target and focuses its query, once its content is mounted. */
  const claimFocus = () => {
    const input = inputRef.current;
    const content = input?.closest(QUICK_PICK_DIALOG_SELECTOR);
    if (!input || !content) return;
    returnFocusTargets.set(content, returnFocusRef.current);
    input.focus();
  };
  useLayoutEffect(() => {
    if (!open) {
      // Closed from outside, such as by the shortcut that opened it: nothing pending applies.
      if (!ownership.closing()) ownership.revoke();
      return;
    }
    // A new opening, even one during the last one's exit, owns nothing from before: an earlier
    // accept's result or close action would act on a picker the user came back to.
    ownership.revoke();
    const active = document.activeElement;
    // Opened in another picker's place: return where that one would.
    const replaced = active?.closest(QUICK_PICK_DIALOG_SELECTOR);
    returnFocusRef.current = replaced
      ? (returnFocusTargets.get(replaced) ?? null)
      : active instanceof HTMLElement
        ? active
        : null;
  }, [open, ownership]);
  // Reopened during its exit animation, the content survived, so Radix won't autofocus it again.
  useEffect(() => {
    if (open) claimFocus();
  }, [open]);
  /** Close for `token`'s result, unless another acceptance, a cancel or a reopen took over. */
  const settle = (token: AcceptanceToken, result: QuickPickAcceptResult) => {
    if (!ownership.isCurrent(token) || result === "keepOpen") return;
    ownership.requestClose(token, result?.afterClose ?? null);
    setClosesAsked((count) => count + 1);
    latestOnOpenChange.current(false);
  };
  const begin = (result: QuickPickDialogAcceptResult): QuickPickAcceptResult => {
    const token = ownership.begin();
    if (isPromiseLike<QuickPickAcceptResult>(result)) {
      // A failed result leaves the picker open for the caller to say why.
      result.then(
        (settled) => settle(token, settled),
        () => undefined,
      );
      return "keepOpen";
    }
    settle(token, result);
    return result === "keepOpen" ? result : undefined;
  };
  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      // A cancel closes with no choice: whatever an accept left pending is dropped.
      ownership.revoke();
      onCancel?.();
    }
    onOpenChange(nextOpen);
  };
  const { onBack } = quickPickProps;
  // Leaving a page leaves what its rows were waiting on, whichever way the user goes back.
  const goBack =
    onBack &&
    (() => {
      ownership.revoke();
      onBack();
    });
  const picker = (
    <QuickPick
      {...quickPickProps}
      onBack={goBack}
      onAccept={(item, details) => begin(onAccept(item, details))}
      onAlternate={onAlternate && (() => begin(onAlternate()))}
      onClose={() => handleOpenChange(false)}
      inputRef={inputRef}
      autoFocus={false}
    />
  );
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-quick-pick-dialog=""
        showCloseButton={false}
        overlayClassName={overlayClassName}
        // On a child page, Escape goes back wherever focus is in the dialog, such as on Back.
        onEscapeKeyDown={(event) => {
          // Escape that ends an IME composition only cancels the composition.
          if (isImeCompositionKeyEvent({ nativeEvent: event })) {
            event.preventDefault();
            return;
          }
          if (!goBack) return;
          event.preventDefault();
          goBack();
          inputRef.current?.focus();
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          // A fresh content mounts after `open` changes, so it's known only here.
          claimFocus();
        }}
        onCloseAutoFocus={(event) => {
          const target = returnFocusRef.current;
          // Only the close the current acceptance asked for runs its action.
          const afterClose = ownership.takeAfterClose();
          returnFocusRef.current = null;
          // An action the choice ran moved focus while this closed: it stays there.
          const active = document.activeElement;
          const movedOn =
            active instanceof HTMLElement &&
            active !== document.body &&
            !active.closest(QUICK_PICK_DIALOG_SELECTOR);
          if (movedOn && !afterClose) {
            event.preventDefault();
            return;
          }
          // A picker opened in this one's place already holds focus and the return target.
          const restore = target?.isConnected && !isBehindOpenLayer(target) ? target : null;
          if (afterClose || target?.isConnected) event.preventDefault();
          // Restore first, so a picker the action opens returns focus here too.
          restore?.focus();
          afterClose?.();
        }}
        className={cn(
          "top-[12vh] translate-y-0 gap-0 overflow-hidden rounded-xl p-0 sm:max-w-xl",
          contentClassName,
        )}
      >
        <DialogTitle className="sr-only">{title ?? quickPickProps.label}</DialogTitle>
        <DialogDescription className="sr-only">
          {description ?? "Type to filter, use the arrow keys to choose, and press Enter."}
        </DialogDescription>
        {/* The picker keeps its place as the heading comes and goes, so its query stays focused. */}
        {heading && (
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <span
              data-slot="quick-pick-heading"
              className="inline-flex items-center gap-1.5 rounded-md bg-muted px-2 py-0.5 text-sm font-medium text-foreground"
            >
              {heading.label}
              {heading.shortcut && <CompactShortcutKeys keys={heading.shortcut} />}
            </span>
            {heading.end && <div className="ml-auto flex items-center">{heading.end}</div>}
          </div>
        )}
        {picker}
      </DialogContent>
    </Dialog>
  );
}
