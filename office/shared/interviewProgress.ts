import type { InterviewQuestion } from "./workflowTypes.ts";

/** Mutually exclusive progress segments of one interview, in bar order. */
export const INTERVIEW_SEGMENTS = [
  { id: "answered", label: "Answered" },
  { id: "awaiting", label: "Awaiting your answer" },
  { id: "upcoming", label: "To be answered" },
  { id: "added", label: "Newly added" },
  { id: "skipped", label: "Skipped" },
] as const;

export type InterviewSegment = (typeof INTERVIEW_SEGMENTS)[number]["id"];

export function interviewSegment(question: InterviewQuestion): InterviewSegment {
  if (question.status === "answered") return "answered";
  if (question.status === "asked") return "awaiting";
  if (question.status === "skipped") return "skipped";
  return question.origin === "added" ? "added" : "upcoming";
}

export interface InterviewProgress {
  counts: Record<InterviewSegment, number>;
  total: number;
  /** True once every question is answered or skipped. */
  complete: boolean;
}

export function interviewProgress(questions: InterviewQuestion[]): InterviewProgress {
  const counts: Record<InterviewSegment, number> = { answered: 0, awaiting: 0, upcoming: 0, added: 0, skipped: 0 };
  for (const question of questions) counts[interviewSegment(question)]++;
  const total = questions.length;
  return { counts, total, complete: total > 0 && counts.answered + counts.skipped === total };
}
