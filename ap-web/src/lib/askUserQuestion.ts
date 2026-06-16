/**
 * Helpers for parsing Claude Code's built-in ``AskUserQuestion`` tool
 * out of a ``PermissionRequest`` elicitation's ``content_preview``
 * (legacy fallback) or its structured ``ask_user_question`` extra
 * (preferred — the server stamps the full parsed payload directly
 * on the elicitation params, bypassing the 1024-char preview cap).
 *
 * Empirically (confirmed against the deployed Claude build): the
 * built-in ``AskUserQuestion`` tool does NOT fire a dedicated hook
 * event — neither ``hooks["AskUserQuestion"]`` nor
 * ``hooks["Elicitation"]``. What fires is the ordinary
 * ``PermissionRequest`` hook, because AskUserQuestion is a tool
 * that needs permission to run. The server-side endpoint detects
 * the AskUserQuestion tool name, parses ``tool_input.questions``
 * into a typed structure, and surfaces it as a params extra so the
 * UI can render it without re-parsing JSON strings.
 *
 * The selection itself doesn't propagate back to Claude (the
 * PermissionRequest hook can only return allow/deny), so the actual
 * answer still flows through Claude's TUI picker — the web form is
 * a nicer cosmetic display + a clean approval surface.
 */

/**
 * One option in a Claude AskUserQuestion picker.
 *
 * Field names + semantics mirror Claude Code's tool input verbatim
 * so a payload posted by Claude can be cast directly without field
 * renaming. ``description`` and ``label`` are required in Claude's
 * wire format; ``preview`` is an optional longer/alternative
 * snippet some Claude builds attach for richer rendering.
 */
export interface ClaudeQuestionOption {
  label: string;
  description?: string;
  preview?: string;
}

/**
 * One question in a Claude AskUserQuestion call.
 *
 * Field names + semantics mirror Claude Code's tool input verbatim.
 * ``header`` is the short category badge Claude attaches
 * (e.g. ``"Framework"``); ``multiSelect`` decides between radio
 * inputs and checkboxes in the form.
 */
export interface ClaudeQuestion {
  id?: string;
  question: string;
  header?: string;
  options: ClaudeQuestionOption[];
  multiSelect: boolean;
  isOther?: boolean;
  isSecret?: boolean;
}

export interface AskUserQuestionPayload {
  questions: ClaudeQuestion[];
}

/**
 * Cast a server-stamped ``ask_user_question`` payload from the
 * elicitation params extras to the typed shape.
 *
 * The server's ``_structured_ask_user_question`` helper builds a
 * clean shape directly from Claude's ``tool_input``, so the only
 * thing the UI checks here is that ``questions`` is an array
 * (defense against a totally malformed extra). Returns ``null``
 * when the payload is missing or doesn't have the top-level
 * ``questions`` array — the caller falls back to parsing the
 * (truncated) ``content_preview`` JSON string.
 */
export function castAskUserQuestionPayload(
  raw: Record<string, unknown> | null | undefined,
): AskUserQuestionPayload | null {
  if (!raw) return null;
  if (!Array.isArray(raw.questions)) return null;
  return raw as unknown as AskUserQuestionPayload;
}

const PREVIEW_PREFIX = "AskUserQuestion(";

/**
 * Try to parse a ``content_preview`` string as an AskUserQuestion
 * payload (legacy fallback for pre-structured-extra elicitations).
 *
 * The expected shape is exactly:
 *
 * ```
 * AskUserQuestion({"questions": [{...}, {...}, ...]})
 * ```
 *
 * Returns ``null`` for any preview that isn't this shape — callers
 * fall back to the generic JSON-blob render for non-AskUserQuestion
 * elicitations.
 *
 * Missing-but-required fields (``header``, ``description``) default
 * to ``""`` rather than dropping the surrounding question/option,
 * so empty Claude metadata doesn't blank the form. The renderer
 * conditionally hides empty values.
 *
 * @param preview The ``params.content_preview`` from a
 *   ``response.elicitation_request`` SSE event.
 * @returns The parsed payload, or ``null`` when the preview isn't
 *   an AskUserQuestion shape.
 */
export function parseAskUserQuestionPreview(preview: string): AskUserQuestionPayload | null {
  if (!preview.startsWith(PREVIEW_PREFIX)) return null;
  if (!preview.endsWith(")")) return null;
  const jsonText = preview.slice(PREVIEW_PREFIX.length, -1);

  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonText);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;

  const questionsRaw = (parsed as Record<string, unknown>).questions;
  if (!Array.isArray(questionsRaw) || questionsRaw.length === 0) return null;

  const questions: ClaudeQuestion[] = [];
  for (const entry of questionsRaw) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const rec = entry as Record<string, unknown>;
    const question = rec.question;
    if (typeof question !== "string" || !question) continue;
    const optionsRaw = rec.options;
    if (!Array.isArray(optionsRaw)) continue;
    const options: ClaudeQuestionOption[] = [];
    for (const opt of optionsRaw) {
      if (!opt || typeof opt !== "object" || Array.isArray(opt)) continue;
      const optRec = opt as Record<string, unknown>;
      const label = optRec.label;
      if (typeof label !== "string" || !label) continue;
      const description = optRec.description;
      const preview = optRec.preview;
      const option: ClaudeQuestionOption = {
        label,
        description: typeof description === "string" ? description : "",
      };
      if (typeof preview === "string" && preview) {
        option.preview = preview;
      }
      options.push(option);
    }
    if (options.length === 0) continue;
    const header = rec.header;
    const id = rec.id;
    const questionPayload: ClaudeQuestion = {
      question,
      header: typeof header === "string" ? header : "",
      options,
      multiSelect: rec.multiSelect === true,
    };
    if (typeof id === "string" && id) {
      questionPayload.id = id;
    }
    questions.push(questionPayload);
  }

  if (questions.length === 0) return null;
  return { questions };
}
