import type { RefObject } from "react";
import { NOT_HANDLED, useRegisterAction, type ActionResult, type ActionSource } from "@/actions";

interface ComposerActionBindingsProps {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  isComposing: () => boolean;
  onSend: (source: ActionSource) => ActionResult;
  onStop: () => ActionResult;
  onRecallPrevious: () => ActionResult;
  onRecallNext: () => ActionResult;
  onSelectPreviousSuggestion: () => ActionResult;
  onSelectNextSuggestion: () => ActionResult;
  onAcceptSuggestion: (behavior: "openOrAttach" | "attach") => ActionResult;
  onDismissSuggestions: () => ActionResult;
}

/** Registers composer commands inside the nearest composer ActionScope. */
export function ComposerActionBindings({
  textareaRef,
  isComposing,
  onSend,
  onStop,
  onRecallPrevious,
  onRecallNext,
  onSelectPreviousSuggestion,
  onSelectNextSuggestion,
  onAcceptSuggestion,
  onDismissSuggestions,
}: ComposerActionBindingsProps) {
  const keyboardIsOwned = (invocation: { source: string; event?: KeyboardEvent }): boolean =>
    invocation.source !== "keyboard" ||
    (!isComposing() && invocation.event?.target === textareaRef.current);
  const run =
    (handler: () => ActionResult) =>
    (invocation: { source: string; event?: KeyboardEvent }): ActionResult =>
      keyboardIsOwned(invocation) ? handler() : NOT_HANDLED;

  useRegisterAction("composer.action.send", {
    acceptsKeybindings: true,
    run: (invocation) => (keyboardIsOwned(invocation) ? onSend(invocation.source) : NOT_HANDLED),
  });
  useRegisterAction("composer.action.stop", {
    acceptsKeybindings: true,
    run: run(onStop),
  });
  useRegisterAction("composer.action.recallPrevious", {
    acceptsKeybindings: true,
    run: run(onRecallPrevious),
  });
  useRegisterAction("composer.action.recallNext", {
    acceptsKeybindings: true,
    run: run(onRecallNext),
  });
  useRegisterAction("composer.action.selectPreviousSuggestion", {
    acceptsKeybindings: true,
    run: run(onSelectPreviousSuggestion),
  });
  useRegisterAction("composer.action.selectNextSuggestion", {
    acceptsKeybindings: true,
    run: run(onSelectNextSuggestion),
  });
  useRegisterAction("composer.action.acceptSuggestion", {
    acceptsKeybindings: true,
    run: (invocation) =>
      keyboardIsOwned(invocation) ? onAcceptSuggestion(invocation.args.behavior) : NOT_HANDLED,
  });
  useRegisterAction("composer.action.dismissSuggestions", {
    acceptsKeybindings: true,
    run: run(onDismissSuggestions),
  });
  return null;
}
