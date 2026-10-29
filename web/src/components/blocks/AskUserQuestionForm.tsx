// Question carousel for structured elicitation, with single- or multi-select answers.
// isOther controls custom input; submit requires an answer to every question.
// Keyboard: Enter advances or submits, 1-9 pick options, Esc cancels. Keys pressed
// outside the form reach it only when keyboardActive is set.

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { type ChangeEvent, useEffect, useRef, useState } from "react";
import { ENTER_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { isDraftingTarget } from "@/hooks/useApproveHotkey";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isFocusInDialogOrMenu, isTextEntry } from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";

/**
 * Map from question id/text → either a single selected label
 * (single-select) or a list of labels (multi-select).
 */
export type AskUserQuestionAnswers = Record<string, string | string[]>;

interface AskUserQuestionFormProps {
  questions: ClaudeQuestion[];
  onSubmit: (answers: AskUserQuestionAnswers) => void;
  onReject: () => void;
  /**
   * Whether keys pressed outside the form drive it. Only the question the user
   * is answering sets this; other forms respond only while focus is inside them.
   */
  keyboardActive?: boolean;
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
// Buttons and links inside the form, which Enter activates natively.
const NATIVE_CLICK_SELECTOR = "button, a[href], [role='button'], [role='link']";
export function AskUserQuestionForm({
  questions,
  onSubmit,
  onReject,
  keyboardActive = false,
}: AskUserQuestionFormProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLFieldSetElement>(null);
  const customInputRef = useRef<HTMLTextAreaElement>(null);
  // Set by a keystroke inside the form that moves to another question, so focus
  // follows the question instead of dropping to the page.
  const focusQuestionOnMoveRef = useRef(false);
  const isMobileViewport = useIsMobileViewport();
  const isCoarsePointer = useIsCoarsePointer();
  // Touch and narrow viewports keep Enter as the newline key, as the composer does.
  const preventsKeyboardSubmit = isMobileViewport || isCoarsePointer;
  const showKeyHints = !preventsKeyboardSubmit;
  // The window listener is registered once and calls the latest handler through this ref.
  const handleKeyRef = useRef<(event: KeyboardEvent) => void>(() => {});

  // Currently-visible question (carousel index).
  const [currentIndex, setCurrentIndex] = useState(0);

  // Per-question option selection. Single-select stores a string;
  // multi-select stores a deduped array.
  const [selections, setSelections] = useState<Record<string, string | string[]>>(() => {
    const initial: Record<string, string | string[]> = {};
    for (const q of questions) {
      initial[questionKey(q)] = q.multiSelect ? [] : "";
    }
    return initial;
  });

  // Per-question custom-input state. ``text`` is what the user
  // typed; ``selected`` is whether the custom row's radio/checkbox
  // is checked (which determines whether the typed text is part of
  // the answer).
  const [customSelected, setCustomSelected] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const q of questions) initial[questionKey(q)] = q.textInput !== undefined;
    return initial;
  });
  const [customInputs, setCustomInputs] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const q of questions) initial[questionKey(q)] = q.textInput?.default ?? "";
    return initial;
  });

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

  const handleCustomInput = (key: string, e: ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value;
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

  const handleSubmit = () => {
    const finalAnswers: AskUserQuestionAnswers = {};
    for (const q of questions) {
      const key = questionKey(q);
      const answer = answerForQuestion(
        q,
        selections[key] ?? "",
        customSelected[key] ?? false,
        customInputs[key] ?? "",
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
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (isImeCompositionKeyEvent({ nativeEvent: event }) || isFocusInDialogOrMenu()) return;
    // An open /btw side answer is dismissed by Escape, so it keeps every key.
    if (document.querySelector("[data-btw-overlay]") !== null) return;

    // Consumes the key so the composer and page handlers do not act on it too.
    const claim = (action: () => void) => {
      event.preventDefault();
      event.stopPropagation();
      // Auto-repeat is consumed without repeating the action.
      if (!event.repeat) action();
    };
    // The chat composer. Side-chat composers are excluded and count as other inputs.
    const composer =
      !inside && target instanceof HTMLTextAreaElement
        ? target.closest<HTMLElement>("[data-composer-card]:not([data-side-chat])")
        : null;
    const suggestionsOpen = composer?.dataset.suggestionsOpen === "true";

    if (event.key === "Enter") {
      if (event.shiftKey || preventsKeyboardSubmit) return;
      if (inside) {
        if (target.matches(NATIVE_CLICK_SELECTOR)) return;
      } else if (composer) {
        // The composer keeps Enter for sending, or for a newline under the Mod+Enter setting.
        if (suggestionsOpen || isDraftingTarget(target) || readSubmitWithModEnter()) return;
      } else if (target.matches(ENTER_OWNING_SELECTOR)) {
        return;
      }
      claim(() => {
        if (allAnswered) {
          handleSubmit();
          return;
        }
        const nextIndex =
          currentIndex === questions.length - 1 ? firstUnansweredIndex : currentIndex + 1;
        if (nextIndex === currentIndex) return;
        focusQuestionOnMoveRef.current = inside;
        setCurrentIndex(nextIndex);
      });
      return;
    }

    if (event.key === "Escape") {
      // Other text fields and open composer menus keep Escape for themselves.
      if (!inside && (composer ? suggestionsOpen : isTextEntry(target))) return;
      claim(onReject);
      return;
    }

    if (DIGIT_KEY.test(event.key)) {
      const digit = Number(event.key);
      const optionCount = activeQuestion.options.length;
      const customDigit = activeQuestion.isOther !== false ? optionCount + 1 : null;
      if (digit > optionCount && digit !== customDigit) return;
      if (isTextEntry(target)) return;
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

  return (
    <div
      ref={rootRef}
      className="flex flex-col gap-2 text-foreground"
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
                    className="flex flex-1 items-start gap-2 cursor-pointer text-ui text-foreground"
                  >
                    <input
                      type="checkbox"
                      id={inputId}
                      checked={checked}
                      onChange={() => handleMultiToggle(currentKey, opt.label)}
                      className="mt-1"
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
                  className="flex flex-1 items-start gap-2 cursor-pointer text-ui text-foreground"
                >
                  <input
                    type="radio"
                    id={inputId}
                    name={currentKey}
                    checked={checked}
                    onChange={() => handleSingleSelect(currentKey, opt.label)}
                    className="mt-1"
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
            <div className="flex items-start gap-2">
              <label
                htmlFor={customRowId}
                className="flex flex-1 items-start gap-2 cursor-pointer text-ui text-foreground"
              >
                <input
                  type={current.multiSelect ? "checkbox" : "radio"}
                  id={customRowId}
                  name={current.multiSelect ? undefined : currentKey}
                  checked={customRowChecked}
                  onChange={() =>
                    current.multiSelect
                      ? handleCustomToggleMulti(currentKey)
                      : handleCustomToggleSingle(currentKey)
                  }
                  className="mt-1"
                  data-testid="ask-user-question-custom-toggle"
                />
                {/* ``field-sizing-content`` (Tailwind v4 / CSS) auto-grows
                    the textarea to fit its content, so typing past the
                    first line wraps and expands downward instead of
                    scrolling within a fixed single row. */}
                <textarea
                  ref={customInputRef}
                  rows={1}
                  placeholder="Type something"
                  value={customRowValue}
                  onChange={(e) => handleCustomInput(currentKey, e)}
                  data-testid="ask-user-question-custom-input"
                  className="field-sizing-content flex-1 resize-none bg-transparent text-ui placeholder:text-muted-foreground focus:outline-none"
                />
              </label>
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

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setCurrentIndex((i) => i - 1)}
          disabled={isFirst}
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
            data-testid="ask-user-question-next"
            componentId="question.next"
          >
            Next
            <ChevronRightIcon className="ml-1 size-3.5" />
            {showKeyHints && !allAnswered && (
              <CompactShortcutKeys keys={[ENTER_KEY]} className="ml-1.5" />
            )}
          </Button>
        )}
        {isLast && (
          <Button
            size="sm"
            onClick={handleSubmit}
            disabled={!allAnswered}
            data-testid="ask-user-question-submit"
            componentId="question.submit"
          >
            <CheckIcon className="mr-1 size-3.5" />
            Submit
            {showKeyHints && allAnswered && (
              <CompactShortcutKeys keys={[ENTER_KEY]} className="ml-1.5" />
            )}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={onReject}
          className="ml-auto"
          componentId="question.cancel"
        >
          <XIcon className="mr-1 size-3.5" />
          Cancel
          {showKeyHints && <CompactShortcutKeys keys={["Esc"]} className="ml-1.5" />}
        </Button>
      </div>
    </div>
  );
}
