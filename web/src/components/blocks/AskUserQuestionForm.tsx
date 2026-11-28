// Question carousel for structured elicitation, with single- or multi-select answers.
// isOther controls custom input; submit requires an answer to every question.
//
// Keyboard: Enter advances or submits, 1-9 pick options, Esc cancels. Keys pressed
// outside the form reach it only when keyboardActive is set.
//
// Unsent answers are restored on remount (see ``@/lib/askUserQuestionDrafts``).

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { DisabledActionTooltip } from "@/components/DisabledActionTooltip";
import { ENTER_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { isDraftingTarget } from "@/hooks/useApproveHotkey";
import { useRequestedComposerEngine } from "@/components/composer/engine/composerEnginePolicy";
import { useComposerEngineHandoff } from "@/components/composer/engine/useComposerEngineHandoff";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import {
  type AskUserQuestionDraft,
  askUserQuestionSignature,
  clearAskUserQuestionDraft,
  getAskUserQuestionDraft,
  saveAskUserQuestionDraft,
} from "@/lib/askUserQuestionDrafts";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import {
  isEscapeOwnerOpen,
  isFocusInDialogOrMenu,
  isKeyOwningOverlayOpen,
  isTextEntry,
} from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { presentTaggedMentions, type SessionMention } from "@/lib/composerMentions";
import {
  type ComposerTextTarget,
  isComposerInput,
} from "@/components/composer/engine/composerInput";
import {
  AnswerMentionScope,
  AskUserQuestionAnswerEditor,
  expandAnswerMentions,
} from "./AskUserQuestionAnswerEditor";

/**
 * Map from question id/text → either a single selected label
 * (single-select) or a list of labels (multi-select).
 */
export type AskUserQuestionAnswers = Record<string, string | string[]>;

interface AskUserQuestionFormProps {
  /** Pending elicitation the questions belong to; keys the unsent draft. */
  elicitationId: string;
  questions: ClaudeQuestion[];
  onSubmit: (answers: AskUserQuestionAnswers) => void;
  onReject: () => void;
  /**
   * Whether keys pressed outside the form drive it. Only the question the user
   * is answering sets this; other forms respond only while focus is inside them.
   */
  keyboardActive?: boolean;
  /**
   * Requires a deliberate answer, as a workflow question does: the card opens with no
   * option selected, and Enter pressed outside the form never submits it. Without this,
   * a stray Enter in an open chat sent the preselected first option, which nobody chose.
   */
  deliberateAnswer?: boolean;
}

/**
 * Derive the final answer for a question given the current state.
 * Returns ``null`` when the question is unanswered.
 *
 * Single-select: when the custom row is selected, the custom text
 * is the answer (and must be non-empty). Otherwise the selected
 * option label is the answer. Text editors preserve whitespace and allow empty text.
 *
 * Multi-select: the answer is the union of selected option labels
 * and (the custom text, when the custom row is selected and the
 * text is non-empty). Deduped so a typed value matching an already-
 * checked option doesn't appear twice.
 */
function answerForQuestion(
  question: ClaudeQuestion,
  selection: string | string[],
  customSelected: boolean,
  customText: string,
): string | string[] | null {
  const customValue = customText.trim();
  if (question.multiSelect) {
    const selected = Array.isArray(selection) ? selection : [];
    const all =
      customSelected && customValue ? Array.from(new Set([...selected, customValue])) : selected;
    return all.length > 0 ? all : null;
  }
  if (customSelected) {
    if (question.textInput) return customText;
    return customValue || null;
  }
  return typeof selection === "string" && selection ? selection : null;
}

function questionKey(question: ClaudeQuestion): string {
  return question.id && question.id.length > 0 ? question.id : question.question;
}

// Digit keys 1-9 pick the first nine rows: the options, then the custom row.
const DIGIT_SHORTCUT_COUNT = 9;
const DIGIT_KEY = /^[1-9]$/;
// Focused controls that keep Enter for their own activation or typing.
const ENTER_OWNING_SELECTOR =
  "a[href], button, input, select, textarea, summary, [contenteditable]:not([contenteditable='false']), [role='button'], [role='link'], [role='checkbox'], [role='radio'], [role='switch'], [role='tab'], [role='menuitem'], [role='option'], [role='combobox'], [role='textbox'], [role='searchbox'], [role='slider']";
// A live dictation take, which commits on Enter and discards on Escape.
const DICTATION_ACTIVE = "[data-dictating]";
// Buttons and links inside the form, which Enter activates natively.
const NATIVE_CLICK_SELECTOR = "button, a[href], [role='button'], [role='link']";
/** Answers on a freshly opened card, before the user touches it. */
function initialAnswers(
  questions: ClaudeQuestion[],
  preselect: boolean,
): Omit<AskUserQuestionDraft, "currentIndex"> {
  const answers: Omit<AskUserQuestionDraft, "currentIndex"> = {
    selections: {},
    customSelected: {},
    customInputs: {},
    sessionMentions: {},
  };
  for (const q of questions) {
    const key = questionKey(q);
    if (q.multiSelect) answers.selections[key] = [];
    else answers.selections[key] = preselect ? (q.options[0]?.label ?? "") : "";
    answers.customSelected[key] = q.textInput !== undefined;
    answers.customInputs[key] = q.textInput?.default ?? "";
  }
  return answers;
}

export function AskUserQuestionForm({
  elicitationId,
  questions,
  onSubmit,
  onReject,
  keyboardActive = false,
  deliberateAnswer = false,
}: AskUserQuestionFormProps) {
  // Form state belongs to the first questions; ApprovalCard remounts the form for new ones.
  const [draftQuestions] = useState(questions);
  const [signature] = useState(() => askUserQuestionSignature(questions));
  const [draft] = useState(() => getAskUserQuestionDraft(elicitationId, signature));
  const [initial] = useState(() => initialAnswers(questions, !deliberateAnswer));
  const rootRef = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLFieldSetElement>(null);
  const customInputRef = useRef<ComposerTextTarget | null>(null);
  // The textarea's caret and focus, carried into the prompt editor that replaces it.
  const textareaHandoffRef = useRef<{
    key: string;
    focused: boolean;
    start: number;
    end: number;
  } | null>(null);
  // Set by a keystroke inside the form that moves to another question, so focus
  // follows the question instead of dropping to the page.
  const focusQuestionOnMoveRef = useRef(false);
  const isMobileViewport = useIsMobileViewport();
  // The prompt editor only where the chat composer uses it (flag on, desktop, chunk loaded).
  // The editor replaces the textarea once its chunk loads, but not while the user is composing.
  const engine = useComposerEngineHandoff(useRequestedComposerEngine(), rootRef, {
    draftKey: elicitationId,
  });
  // The mobile layout keeps Enter as the newline key, as the composer does.
  const preventsKeyboardSubmit = isMobileViewport;
  const showKeyHints = !preventsKeyboardSubmit;
  // The window listener is registered once and calls the latest handler through this ref.
  const handleKeyRef = useRef<(event: KeyboardEvent) => void>(() => {});
  // Arrow keys check each radio they pass, which doesn't mean the custom row was chosen.
  const radioArrowKeyRef = useRef(false);
  const isEmbedded = useIsEmbedded();

  // Currently-visible question (carousel index).
  const [currentIndex, setCurrentIndex] = useState(draft?.currentIndex ?? 0);
  // Questions shown so far, so Enter doesn't submit default answers to unseen ones.
  const [seenIndexes, setSeenIndexes] = useState<ReadonlySet<number>>(
    () => new Set([draft?.currentIndex ?? 0]),
  );
  if (!seenIndexes.has(currentIndex)) setSeenIndexes(new Set([...seenIndexes, currentIndex]));
  const allSeen = questions.every((_question, index) => seenIndexes.has(index));

  // Per-question option selection. Single-select stores a string;
  // multi-select stores a deduped array.
  const [selections, setSelections] = useState(draft?.selections ?? initial.selections);

  // Per-question custom-input state. ``customInputs`` is what the user
  // typed; ``customSelected`` is whether the custom row's radio/checkbox
  // is checked (which determines whether the typed text is part of
  // the answer).
  const [customSelected, setCustomSelected] = useState(
    draft?.customSelected ?? initial.customSelected,
  );
  const [customInputs, setCustomInputs] = useState(draft?.customInputs ?? initial.customInputs);
  // Session tags per question: each question's scope reads its entry and writes it back here.
  const currentQuestionKey = questions[currentIndex] ? questionKey(questions[currentIndex]) : "";
  const [sessionMentionsByKey, setSessionMentionsByKey] = useState<
    Record<string, SessionMention[]>
  >(draft?.sessionMentions ?? {});
  const rememberTextareaCaret = (element: HTMLTextAreaElement) => {
    textareaHandoffRef.current = {
      key: currentQuestionKey,
      focused: document.activeElement === element,
      start: element.selectionStart,
      end: element.selectionEnd,
    };
  };
  // After the editor replaces the textarea, the user's caret and focus go with it.
  useLayoutEffect(() => {
    const snapshot = textareaHandoffRef.current;
    textareaHandoffRef.current = null;
    const editor = customInputRef.current;
    if (engine !== "codemirror" || !snapshot || snapshot.key !== currentQuestionKey || !editor)
      return;
    editor.setSelectionRange(snapshot.start, snapshot.end);
    if (snapshot.focused) editor.focus();
  }, [engine, currentQuestionKey]);

  useEffect(() => {
    const answers = {
      selections: { ...selections },
      customSelected: { ...customSelected },
      customInputs: { ...customInputs },
      sessionMentions: {} as Record<string, SessionMention[]>,
    };
    // Answers to a secret question are never kept past this mount, nor their session tags.
    for (const q of draftQuestions) {
      const key = questionKey(q);
      if (!q.isSecret) {
        // Only tags whose token still shows in their own answer are kept.
        const kept = presentTaggedMentions(
          { files: [], sessions: sessionMentionsByKey[key] ?? [] },
          [customInputs[key] ?? ""],
        )?.sessions;
        if (kept) answers.sessionMentions[key] = kept;
        continue;
      }
      answers.selections[key] = initial.selections[key]!;
      answers.customSelected[key] = initial.customSelected[key]!;
      answers.customInputs[key] = initial.customInputs[key]!;
    }
    // Untouched cards keep no draft, so they cannot evict real ones.
    if (currentIndex === 0 && JSON.stringify(answers) === JSON.stringify(initial)) {
      clearAskUserQuestionDraft(elicitationId, signature);
      return;
    }
    saveAskUserQuestionDraft(elicitationId, signature, { currentIndex, ...answers });
  }, [
    elicitationId,
    draftQuestions,
    signature,
    initial,
    currentIndex,
    selections,
    customSelected,
    customInputs,
    sessionMentionsByKey,
  ]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => handleKeyRef.current(event);
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, []);

  // Focus stays in the form when a keystroke moves to another question.
  useEffect(() => {
    if (!focusQuestionOnMoveRef.current) return;
    focusQuestionOnMoveRef.current = false;
    questionRef.current?.focus({ preventScroll: true });
  }, [currentIndex]);

  const handleSingleSelect = (key: string, label: string) => {
    // Single-select mutex: clicking a real option clears the
    // custom row selection so the radio group exposes exactly one
    // chosen value.
    setSelections((prev) => ({ ...prev, [key]: label }));
    setCustomSelected((prev) => ({ ...prev, [key]: false }));
  };

  const handleCustomToggleSingle = (key: string) => {
    // Selecting the custom row clears the real-option selection
    // for the same single-select mutex reason.
    setCustomSelected((prev) => ({ ...prev, [key]: true }));
    setSelections((prev) => ({ ...prev, [key]: "" }));
  };

  const handleMultiToggle = (key: string, label: string) => {
    setSelections((prev) => {
      const current = prev[key];
      const set = new Set(Array.isArray(current) ? current : []);
      if (set.has(label)) {
        set.delete(label);
      } else {
        set.add(label);
      }
      return { ...prev, [key]: Array.from(set) };
    });
  };

  const handleCustomToggleMulti = (key: string) => {
    setCustomSelected((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const handleCustomInput = (key: string, text: string) => {
    setCustomInputs((prev) => ({ ...prev, [key]: text }));
    // Typing implies intent to use the custom answer — auto-check
    // the custom radio/checkbox so the user doesn't have to click
    // twice. Clearing the text doesn't auto-uncheck (the user may
    // want to retype).
    if (text && !customSelected[key]) {
      const question = questions.find((q) => questionKey(q) === key);
      if (question && !question.multiSelect) {
        handleCustomToggleSingle(key);
      } else {
        setCustomSelected((prev) => ({ ...prev, [key]: true }));
      }
    }
  };

  // Every question needs at least one answer. Without this guard
  // the form submits half-filled answers and the LLM sees ``null``
  // for the missing slots.
  const isAnswered = (question: ClaudeQuestion) => {
    const key = questionKey(question);
    return (
      answerForQuestion(
        question,
        selections[key] ?? "",
        customSelected[key] ?? false,
        customInputs[key] ?? "",
      ) !== null
    );
  };
  const firstUnansweredIndex = questions.findIndex((question) => !isAnswered(question));
  const allAnswered = firstUnansweredIndex === -1;
  // Says which question keeps Submit disabled; it can be on another page.
  const firstUnanswered = questions[firstUnansweredIndex];
  const submitBlockedReason = firstUnanswered
    ? `Answer question ${firstUnansweredIndex + 1}${firstUnanswered.header ? ` (${firstUnanswered.header})` : ""} to submit`
    : undefined;

  const handleSubmit = () => {
    const finalAnswers: AskUserQuestionAnswers = {};
    for (const q of questions) {
      const key = questionKey(q);
      const answer = answerForQuestion(
        q,
        selections[key] ?? "",
        customSelected[key] ?? false,
        expandAnswerMentions(customInputs[key] ?? "", sessionMentionsByKey[key] ?? []),
      );
      if (answer === null) return; // unreachable while ``allAnswered`` gates the button
      finalAnswers[key] = answer;
    }
    onSubmit(finalAnswers);
  };

  // Inside the form every key is handled here. Outside it, only the keyboardActive
  // form takes Enter, Escape, and digits.
  const handleKey = (event: KeyboardEvent) => {
    const target = event.target;
    const activeQuestion = questions[currentIndex];
    if (!activeQuestion || !(target instanceof HTMLElement)) return;
    const inside = rootRef.current?.contains(target) === true;
    if (!inside && !keyboardActive) return;
    if (!inside) {
      // Keys pressed in another question form, or in a side chat this form isn't
      // part of, belong there.
      if (target.closest("[data-ask-user-question-form]")) return;
      const sideChat = target.closest("[data-side-chat-pane]");
      if (sideChat && !sideChat.contains(rootRef.current)) return;
      // Embedded, keys from the host page around the app aren't the app's.
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
    }
    // An open @ menu in the custom answer takes its keys, Escape included, before the form does.
    if (inside && rootRef.current?.querySelector("[data-suggestions-open='true']")) return;
    // An open overlay keeps the keys even when focus wandered back into the form behind it.
    if (isKeyOwningOverlayOpen(rootRef.current)) return;
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (isImeCompositionKeyEvent({ nativeEvent: event }) || isFocusInDialogOrMenu()) return;
    // An open /btw side answer is dismissed by Escape, so it keeps every key.
    if (document.querySelector("[data-btw-overlay]") !== null) return;
    const enterOrEscape = event.key === "Enter" || event.key === "Escape";
    if (enterOrEscape && document.querySelector(DICTATION_ACTIVE) !== null) return;

    // Consumes the key so the composer and page handlers do not act on it too.
    const claim = (action: () => void) => {
      event.preventDefault();
      event.stopPropagation();
      // Auto-repeat is consumed without repeating the action.
      if (!event.repeat) action();
    };
    // The chat composer. Side-chat composers are excluded and count as other inputs.
    const composer =
      !inside && isComposerInput(target)
        ? target.closest<HTMLElement>("[data-composer-card]:not([data-side-chat])")
        : null;
    const suggestionsOpen = composer?.dataset.suggestionsOpen === "true";

    if (event.key === "Enter") {
      if (event.shiftKey || preventsKeyboardSubmit) return;
      // A deliberate answer is given in the form itself, never by an Enter from elsewhere.
      if (deliberateAnswer && !inside) return;
      if (inside) {
        if (target.matches(NATIVE_CLICK_SELECTOR)) return;
      } else if (composer) {
        // The composer keeps Enter for sending, or for a newline under the Mod+Enter setting.
        if (suggestionsOpen || isDraftingTarget(target) || readSubmitWithModEnter()) return;
      } else if (target.matches(ENTER_OWNING_SELECTOR)) {
        return;
      }
      claim(() => {
        const firstUnseenIndex = questions.findIndex((_question, index) => !seenIndexes.has(index));
        if (allAnswered && allSeen) {
          handleSubmit();
          return;
        }
        const nextIndex =
          currentIndex === questions.length - 1
            ? firstUnansweredIndex === -1
              ? firstUnseenIndex
              : firstUnansweredIndex
            : currentIndex + 1;
        if (nextIndex === currentIndex) return;
        focusQuestionOnMoveRef.current = inside;
        setCurrentIndex(nextIndex);
      });
      return;
    }

    if (event.key === "Escape") {
      // Outside the form Escape declines only from the page or an empty composer; open
      // composer menus, drafts, and other focused controls keep it.
      const ownedOutside = composer
        ? suggestionsOpen || isDraftingTarget(target)
        : target.closest(ENTER_OWNING_SELECTOR) !== null;
      // An open panel that closes on Escape keeps it, even from inside the form.
      if (isEscapeOwnerOpen()) return;
      if (!inside && ownedOutside) return;
      claim(onReject);
      return;
    }

    if (DIGIT_KEY.test(event.key)) {
      const digit = Number(event.key);
      const optionCount = activeQuestion.options.length;
      const customDigit = activeQuestion.isOther !== false ? optionCount + 1 : null;
      if (digit > optionCount && digit !== customDigit) return;
      if (isTextEntry(target)) return;
      // Outside the form, a focused control (a tab strip's numbered tabs) keeps its digits.
      if (!inside && target.closest(ENTER_OWNING_SELECTOR)) return;
      claim(() => {
        const activeKey = questionKey(activeQuestion);
        const option = activeQuestion.options[digit - 1];
        if (option) {
          if (activeQuestion.multiSelect) handleMultiToggle(activeKey, option.label);
          else handleSingleSelect(activeKey, option.label);
          return;
        }
        // The digit after the last option picks the custom row and focuses its input.
        if (!(customSelected[activeKey] ?? false)) {
          if (activeQuestion.multiSelect) {
            setCustomSelected((prev) => ({ ...prev, [activeKey]: true }));
          } else {
            handleCustomToggleSingle(activeKey);
          }
        }
        customInputRef.current?.focus();
      });
    }
  };
  handleKeyRef.current = handleKey;

  const current = questions[currentIndex];
  if (!current) return null;
  const currentKey = questionKey(current);
  const isFirst = currentIndex === 0;
  const isLast = currentIndex === questions.length - 1;

  // Selected labels drive the preview render. Only PREDEFINED
  // options contribute previews — the custom row has no preview
  // to show. Single-select: 0 or 1 selected option (custom doesn't
  // contribute). Multi-select: 0 to N selected options.
  const selectedLabels: string[] = current.multiSelect
    ? Array.isArray(selections[currentKey])
      ? (selections[currentKey] as string[])
      : []
    : !customSelected[currentKey] &&
        typeof selections[currentKey] === "string" &&
        selections[currentKey]
      ? [selections[currentKey] as string]
      : [];
  const previewsToShow = current.options.filter(
    (opt) => selectedLabels.includes(opt.label) && opt.preview,
  );

  const allowCustomInput = current.isOther !== false;
  const customRowId = `${currentKey}__custom`;
  const customRowChecked = customSelected[currentKey] ?? false;
  const customRowValue = customInputs[currentKey] ?? "";

  const renderBody = () => (
    <div
      ref={rootRef}
      className="flex flex-col gap-2 text-foreground"
      data-ask-user-question-form=""
      data-testid="ask-user-question-form"
    >
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span data-testid="ask-user-question-progress">
          Question {currentIndex + 1} of {questions.length}:
        </span>
        {current.header && (
          <span className="text-muted-foreground text-sm rounded bg-muted px-1.5 py-0.5">
            {current.header}
          </span>
        )}
      </div>

      <fieldset
        key={currentKey}
        ref={questionRef}
        tabIndex={-1}
        className="flex flex-col gap-2 mb-2 focus:outline-none"
        data-testid="ask-user-question-section"
        onKeyDown={(event) => {
          if (!event.key.startsWith("Arrow")) return;
          radioArrowKeyRef.current = true;
          // The radio changes in this keydown's default action, before the timeout runs.
          setTimeout(() => {
            radioArrowKeyRef.current = false;
          });
        }}
      >
        <legend className="text-foreground text-ui font-medium flex items-center gap-2 mb-2">
          {current.question}
        </legend>
        <div className="flex flex-col gap-2">
          {current.options.map((opt, optionIndex) => {
            const inputId = `${currentKey}-${opt.label}`;
            const keyHint =
              showKeyHints && optionIndex < DIGIT_SHORTCUT_COUNT ? (
                <CompactShortcutKeys keys={[String(optionIndex + 1)]} className="mt-0.5" />
              ) : null;
            if (current.multiSelect) {
              const sel = selections[currentKey];
              const checked = Array.isArray(sel) && sel.includes(opt.label);
              return (
                <div key={opt.label} className="flex items-start gap-2">
                  <label
                    htmlFor={inputId}
                    className="flex flex-1 items-start gap-2 cursor-pointer text-ui text-foreground mobile-touch-target mobile-app:grid mobile-app:grid-cols-[44px_minmax(0,1fr)] mobile-app:items-center"
                  >
                    <input
                      type="checkbox"
                      id={inputId}
                      checked={checked}
                      onChange={() => handleMultiToggle(currentKey, opt.label)}
                      className="mt-1 mobile-app:mt-0 mobile-app:justify-self-center"
                    />
                    <span className="flex flex-col">
                      <span>{opt.label}</span>
                      {opt.description && (
                        <span className="text-muted-foreground text-sm">{opt.description}</span>
                      )}
                    </span>
                  </label>
                  {keyHint}
                </div>
              );
            }
            const sel = selections[currentKey];
            const checked = sel === opt.label && !customSelected[currentKey];
            return (
              <div key={opt.label} className="flex items-start gap-2">
                <label
                  htmlFor={inputId}
                  className="flex flex-1 items-start gap-2 cursor-pointer text-ui text-foreground mobile-touch-target mobile-app:grid mobile-app:grid-cols-[44px_minmax(0,1fr)] mobile-app:items-center"
                >
                  <input
                    type="radio"
                    id={inputId}
                    name={currentKey}
                    checked={checked}
                    onChange={() => handleSingleSelect(currentKey, opt.label)}
                    className="mt-1 mobile-app:mt-0 mobile-app:justify-self-center"
                  />
                  <span className="flex flex-col">
                    <span>{opt.label}</span>
                    {opt.description && (
                      <span className="text-muted-foreground text-sm">{opt.description}</span>
                    )}
                  </span>
                </label>
                {keyHint}
              </div>
            );
          })}
          {allowCustomInput && (
            /* Custom-input row — visually "one more option" with a
               matching radio/checkbox prefix, so the user picks
               between predefined options and free-form input the
               same way. */
            <div className="flex items-start gap-2 text-ui text-foreground mobile-app:grid mobile-app:grid-cols-[44px_minmax(0,1fr)] mobile-app:items-center">
              <label
                htmlFor={customRowId}
                className="flex shrink-0 cursor-pointer mobile-touch-target mobile-app:items-center mobile-app:justify-center"
              >
                <input
                  type={current.multiSelect ? "checkbox" : "radio"}
                  id={customRowId}
                  name={current.multiSelect ? undefined : currentKey}
                  checked={customRowChecked}
                  onChange={() => {
                    if (current.multiSelect) handleCustomToggleMulti(currentKey);
                    else handleCustomToggleSingle(currentKey);
                    // Choosing the custom row means its answer is typed next.
                    if (!customRowChecked && !radioArrowKeyRef.current) {
                      customInputRef.current?.focus();
                    }
                  }}
                  aria-label="Select custom answer"
                  className="mt-1 mobile-app:mt-0"
                  data-testid="ask-user-question-custom-toggle"
                />
              </label>
              <AnswerMentionScope
                key={currentKey}
                currentKey={currentKey}
                customText={customRowValue}
                onCustomTextChange={(text) => handleCustomInput(currentKey, text)}
                initialSessions={sessionMentionsByKey[currentKey] ?? []}
                onSessionsChange={setSessionMentionsByKey}
                isMobile={isMobileViewport}
                inputRef={customInputRef}
              >
                {(mentions) =>
                  engine === "codemirror" ? (
                    <AskUserQuestionAnswerEditor
                      inputRef={customInputRef}
                      value={customRowValue}
                      onValueChange={(text) => handleCustomInput(currentKey, text)}
                      mentions={mentions}
                    />
                  ) : (
                    /* ``field-sizing-content`` (Tailwind v4 / CSS) auto-grows
                  the textarea to fit its content, so typing past the
                  first line wraps and expands downward instead of
                  scrolling within a fixed single row. */
                    <textarea
                      ref={(element) => {
                        customInputRef.current = element;
                      }}
                      rows={1}
                      placeholder="Type something"
                      value={customRowValue}
                      onChange={(e) => {
                        mentions.browser.noteTextChange(e);
                        handleCustomInput(currentKey, e.target.value);
                        rememberTextareaCaret(e.currentTarget);
                      }}
                      onFocus={(e) => rememberTextareaCaret(e.currentTarget)}
                      onSelect={(e) => rememberTextareaCaret(e.currentTarget)}
                      onBlur={(e) => rememberTextareaCaret(e.currentTarget)}
                      data-testid="ask-user-question-custom-input"
                      aria-label="Custom answer"
                      className="mobile-touch-target mobile-app:min-w-0 mobile-app:py-3 field-sizing-content flex-1 resize-none bg-transparent text-ui placeholder:text-muted-foreground focus:outline-none"
                    />
                  )
                }
              </AnswerMentionScope>
              {showKeyHints && current.options.length < DIGIT_SHORTCUT_COUNT && (
                <CompactShortcutKeys
                  keys={[String(current.options.length + 1)]}
                  className="mt-0.5"
                />
              )}
            </div>
          )}
        </div>
        {previewsToShow.length > 0 && (
          <div className="flex flex-col gap-1" data-testid="ask-user-question-previews">
            {previewsToShow.map((opt) => (
              <pre
                key={opt.label}
                className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-sm whitespace-pre-wrap"
              >
                {opt.preview}
              </pre>
            ))}
          </div>
        )}
      </fieldset>

      <div className="flex items-center gap-2 mobile-app:flex-wrap">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setCurrentIndex((i) => i - 1)}
          disabled={isFirst}
          className="mobile-touch-target mobile-app:h-auto mobile-app:whitespace-normal"
          data-testid="ask-user-question-prev"
          componentId="question.prev"
        >
          <ChevronLeftIcon className="mr-1 size-3.5" />
          Prev
        </Button>
        {!isLast && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setCurrentIndex((i) => i + 1)}
            className="mobile-touch-target mobile-app:h-auto mobile-app:whitespace-normal"
            data-testid="ask-user-question-next"
            componentId="question.next"
          >
            Next
            <ChevronRightIcon className="ml-1 size-3.5" />
            {showKeyHints && !(allAnswered && allSeen) && (
              <CompactShortcutKeys keys={[ENTER_KEY]} className="ml-1.5" />
            )}
          </Button>
        )}
        {isLast && (
          <DisabledActionTooltip reason={submitBlockedReason} label="Submit">
            <Button
              size="sm"
              onClick={handleSubmit}
              disabled={!allAnswered}
              className="mobile-touch-target mobile-app:h-auto mobile-app:whitespace-normal"
              data-testid="ask-user-question-submit"
              componentId="question.submit"
            >
              <CheckIcon className="mr-1 size-3.5" />
              Submit
              {showKeyHints && allAnswered && allSeen && (
                <CompactShortcutKeys keys={[ENTER_KEY]} className="ml-1.5" />
              )}
            </Button>
          </DisabledActionTooltip>
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={onReject}
          className="ml-auto mobile-touch-target mobile-app:h-auto mobile-app:whitespace-normal"
          componentId="question.cancel"
        >
          <XIcon className="mr-1 size-3.5" />
          Cancel
          {showKeyHints && <CompactShortcutKeys keys={["Esc"]} className="ml-1.5" />}
        </Button>
      </div>
    </div>
  );
  return renderBody();
}
