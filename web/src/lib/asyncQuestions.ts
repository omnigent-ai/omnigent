import type { ClaudeQuestion } from "./askUserQuestion";

export interface AsyncUserInput {
  call_id: string;
  reply_id: string;
  questions: { title: string; options?: string[] | null }[];
}

export interface AsyncQuestionReply {
  source_item_id: string;
  answers: Record<string, string>;
  cancelled: boolean;
}

export function readAsyncUserInput(raw: unknown): AsyncUserInput | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as AsyncUserInput;
  if (
    typeof value.reply_id !== "string" ||
    !/^[0-9a-f]{32}$/.test(value.reply_id) ||
    typeof value.call_id !== "string" ||
    !value.call_id.trim() ||
    !Array.isArray(value.questions) ||
    value.questions.length === 0
  )
    return undefined;
  if (
    !value.questions.every(
      (q) =>
        q &&
        typeof q.title === "string" &&
        q.title.trim() &&
        (q.options == null ||
          (Array.isArray(q.options) &&
            q.options.length > 0 &&
            q.options.every((o) => typeof o === "string" && o.trim()))),
    )
  )
    return undefined;
  return value;
}

export function readAsyncQuestionReply(raw: unknown): AsyncQuestionReply | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as AsyncQuestionReply;
  if (
    typeof value.source_item_id !== "string" ||
    !value.source_item_id ||
    typeof value.cancelled !== "boolean" ||
    !value.answers ||
    typeof value.answers !== "object" ||
    Array.isArray(value.answers) ||
    !Object.values(value.answers).every((v) => typeof v === "string")
  )
    return undefined;
  return value;
}

export function asyncQuestions(prompt: AsyncUserInput): ClaudeQuestion[] {
  return prompt.questions.map((q, index) => ({
    id: `${prompt.call_id}:${index}`,
    question: q.title,
    options: (q.options ?? []).map((label) => ({ label })),
    multiSelect: false,
  }));
}
