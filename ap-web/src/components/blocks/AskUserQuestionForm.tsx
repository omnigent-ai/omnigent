// Interactive form for Claude Code's built-in ``AskUserQuestion``
// tool. Rendered inside the existing ``ApprovalCard`` when the
// PermissionRequest carries a structured ``ask_user_question``
// payload — see :file:`@/lib/askUserQuestion`.
//
// **Carousel** layout: only one question is visible at a time with
// Prev / Next / Submit navigation. ``Question N of M`` indicator
// keeps progress legible without stacking N fieldsets vertically.
//
// Each question section renders:
//   - the question text in primary foreground color + an optional
//     ``header`` badge in muted text
//   - radio inputs (single-select) or checkboxes (multi-select)
//     for the predefined options, with each option's description
//     under the label in muted text
//   - a "Type something" custom-input row at the bottom of the
//     option list — same radio/checkbox prefix as the real
//     options, so visually it's "one more option" whose value the
//     user types. The radio/checkbox is part of the same group
//     as the predefined options (single-select mutex; multi-select
//     independent toggle).
//   - <pre> blocks showing the ``preview`` of any currently-selected
//     option that has one (single-select: 0 or 1; multi-select:
//     one per selected option with a preview).
//
// Submit is gated on EVERY question having an answer (a predefined
// option selected, or the custom row selected with non-empty text).
// Selections are gathered into a flat ``{[question id or text]:
// answer}`` map matching MCP's ``ElicitResult.content`` shape and
// passed to ``onSubmit``.

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { type ChangeEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";

/**
 * Map from question id/text → either a single selected label
 * (single-select) or a list of labels (multi-select).
 */
export type AskUserQuestionAnswers = Record<string, string | string[]>;

interface AskUserQuestionFormProps {
  questions: ClaudeQuestion[];
  onSubmit: (answers: AskUserQuestionAnswers) => void;
  onReject: () => void;
}

/**
 * Derive the final answer for a question given the current state.
 * Returns ``null`` when the question is unanswered.
 *
 * Single-select: when the custom row is selected, the custom text
 * is the answer (and must be non-empty). Otherwise the selected
 * option label is the answer.
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
    return customValue || null;
  }
  return typeof selection === "string" && selection ? selection : null;
}

function questionKey(question: ClaudeQuestion): string {
  return question.id && question.id.length > 0 ? question.id : question.question;
}

export function AskUserQuestionForm({ questions, onSubmit, onReject }: AskUserQuestionFormProps) {
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
    for (const q of questions) initial[questionKey(q)] = false;
    return initial;
  });
  const [customInputs, setCustomInputs] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const q of questions) initial[questionKey(q)] = "";
    return initial;
  });

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
  const allAnswered = questions.every((q) => {
    const key = questionKey(q);
    return (
      answerForQuestion(
        q,
        selections[key] ?? "",
        customSelected[key] ?? false,
        customInputs[key] ?? "",
      ) !== null
    );
  });

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

  const customRowId = `${currentKey}__custom`;
  const customRowChecked = customSelected[currentKey] ?? false;
  const customRowValue = customInputs[currentKey] ?? "";

  return (
    <div className="flex flex-col gap-2 text-foreground" data-testid="ask-user-question-form">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span data-testid="ask-user-question-progress">
          Question {currentIndex + 1} of {questions.length}:
        </span>
        {current.header && (
          <span className="text-muted-foreground text-xs rounded bg-muted px-1.5 py-0.5">
            {current.header}
          </span>
        )}
      </div>

      <fieldset
        key={currentKey}
        className="flex flex-col gap-2 mb-2"
        data-testid="ask-user-question-section"
      >
        <legend className="text-foreground text-sm font-medium flex items-center gap-2 mb-2">
          {current.question}
        </legend>
        <div className="flex flex-col gap-2">
          {current.options.map((opt) => {
            const inputId = `${currentKey}-${opt.label}`;
            if (current.multiSelect) {
              const sel = selections[currentKey];
              const checked = Array.isArray(sel) && sel.includes(opt.label);
              return (
                <label
                  key={opt.label}
                  htmlFor={inputId}
                  className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
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
                      <span className="text-muted-foreground text-xs">{opt.description}</span>
                    )}
                  </span>
                </label>
              );
            }
            const sel = selections[currentKey];
            const checked = sel === opt.label && !customSelected[currentKey];
            return (
              <label
                key={opt.label}
                htmlFor={inputId}
                className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
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
                    <span className="text-muted-foreground text-xs">{opt.description}</span>
                  )}
                </span>
              </label>
            );
          })}
          {/* Custom-input row — visually "one more option" with a
              matching radio/checkbox prefix, so the user picks
              between predefined options and free-form input the
              same way. */}
          <label
            htmlFor={customRowId}
            className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
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
              rows={1}
              placeholder="Type something"
              value={customRowValue}
              onChange={(e) => handleCustomInput(currentKey, e)}
              data-testid="ask-user-question-custom-input"
              className="field-sizing-content flex-1 resize-none bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
            />
          </label>
        </div>
        {previewsToShow.length > 0 && (
          <div className="flex flex-col gap-1" data-testid="ask-user-question-previews">
            {previewsToShow.map((opt) => (
              <pre
                key={opt.label}
                className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap"
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
          >
            Next
            <ChevronRightIcon className="ml-1 size-3.5" />
          </Button>
        )}
        {isLast && (
          <Button
            size="sm"
            onClick={handleSubmit}
            disabled={!allAnswered}
            data-testid="ask-user-question-submit"
          >
            <CheckIcon className="mr-1 size-3.5" />
            Submit
          </Button>
        )}
        <Button size="sm" variant="outline" onClick={onReject} className="ml-auto">
          <XIcon className="mr-1 size-3.5" />
          Cancel
        </Button>
      </div>
    </div>
  );
}
