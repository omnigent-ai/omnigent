import { z } from "zod";

const partySchema = z.object({ name: z.string().min(2), team: z.string().min(2) }).strict();

export const dpiaRequestSchema = z
  .object({
    artifact: z.literal("dpia-request"),
    request_id: z.string().regex(/^req-[a-z0-9][a-z0-9-]{2,60}$/),
    requester: partySchema,
    project: z
      .object({
        title: z.string().min(3),
        purpose: z.string().min(10),
        data_subjects: z.string().min(3),
        personal_data: z.string().min(3),
        vendors: z.string().min(1),
        timeline: z.string().min(1),
      })
      .strict(),
    known_unknowns: z.array(z.string().min(3)),
    submitted_at: z.string().min(10),
  })
  .strict()
  .superRefine((request, context) => {
    if (new Set(request.known_unknowns).size !== request.known_unknowns.length) {
      context.addIssue({
        code: "custom",
        path: ["known_unknowns"],
        message: "Known unknowns must be unique.",
      });
    }
  });

export const stakeholderResponseSchema = z
  .object({
    artifact: z.literal("stakeholder-response"),
    case_id: z.string().min(1),
    request_id: z.string().min(1).optional(),
    respondent: partySchema,
    answers: z
      .array(z.object({ question_id: z.string().min(1), response: z.string().min(10) }).strict())
      .min(1),
    submitted_at: z.string().min(10),
  })
  .strict()
  .superRefine((response, context) => {
    const questionIds = response.answers.map(({ question_id }) => question_id);
    if (new Set(questionIds).size !== questionIds.length) {
      context.addIssue({
        code: "custom",
        path: ["answers"],
        message: "Answer question ids must be unique.",
      });
    }
  });

export const dpiaOutcomeSchema = z
  .object({
    artifact: z.literal("dpia-outcome"),
    request_id: z.string().min(1),
    case_id: z.string().min(1).optional(),
    decision: z.enum(["approved", "approved-with-conditions", "rejected", "not-required"]),
    reasons: z.array(z.string().min(5)).min(1),
    conditions: z.array(
      z
        .object({
          action: z.string().min(5),
          owner: z.string().min(2),
          due: z.string().min(4),
        })
        .strict(),
    ),
    review_date: z.string().min(4),
    contact: z.string().min(3),
    decided_by: z.string().min(2),
    decided_at: z.string().min(10),
  })
  .strict()
  .superRefine((outcome, context) => {
    if (outcome.decision === "approved-with-conditions" && outcome.conditions.length === 0) {
      context.addIssue({
        code: "custom",
        path: ["conditions"],
        message: "An approval with conditions must list at least one condition.",
      });
    }
  });

export type DpiaRequest = z.infer<typeof dpiaRequestSchema>;
export type StakeholderResponse = z.infer<typeof stakeholderResponseSchema>;
export type DpiaOutcome = z.infer<typeof dpiaOutcomeSchema>;

function parseArtifactText<T>(schema: z.ZodType<T>, text: string): T | null {
  try {
    return schema.parse(JSON.parse(text.trim()));
  } catch {
    return null;
  }
}

export function parseDpiaRequestText(text: string): DpiaRequest | null {
  return parseArtifactText(dpiaRequestSchema, text);
}

export function parseStakeholderResponseText(text: string): StakeholderResponse | null {
  return parseArtifactText(stakeholderResponseSchema, text);
}

export function parseDpiaOutcomeText(text: string): DpiaOutcome | null {
  return parseArtifactText(dpiaOutcomeSchema, text);
}

export function latestArtifact<T>(
  texts: readonly string[],
  parse: (text: string) => T | null,
): T | null {
  for (let index = texts.length - 1; index >= 0; index -= 1) {
    const artifact = parse(texts[index]);
    if (artifact !== null) return artifact;
  }
  return null;
}

export function newDpiaRequestId(title: string, now: Date): string {
  const slug =
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 24)
      .replace(/-+$/g, "") || "project";
  return `req-${slug}-${now.getTime().toString(36)}`;
}

export interface DpiaRequestDraft {
  requesterName: string;
  requesterTeam: string;
  title: string;
  purpose: string;
  dataSubjects: string;
  personalData: string;
  vendors: string;
  timeline: string;
  knownUnknowns: string[];
}

export function buildDpiaRequestArtifact(
  draft: DpiaRequestDraft,
  requestId: string,
  submittedAt: string,
): DpiaRequest {
  return dpiaRequestSchema.parse({
    artifact: "dpia-request",
    request_id: requestId,
    requester: { name: draft.requesterName.trim(), team: draft.requesterTeam.trim() },
    project: {
      title: draft.title.trim(),
      purpose: draft.purpose.trim(),
      data_subjects: draft.dataSubjects.trim(),
      personal_data: draft.personalData.trim(),
      vendors: draft.vendors.trim(),
      timeline: draft.timeline.trim(),
    },
    known_unknowns: draft.knownUnknowns.map((entry) => entry.trim()).filter(Boolean),
    submitted_at: submittedAt,
  });
}

export interface StakeholderResponseDraft {
  caseId: string;
  requestId?: string;
  respondentName: string;
  respondentTeam: string;
  answers: { questionId: string; response: string }[];
}

export function buildStakeholderResponseArtifact(
  draft: StakeholderResponseDraft,
  submittedAt: string,
): StakeholderResponse {
  return stakeholderResponseSchema.parse({
    artifact: "stakeholder-response",
    case_id: draft.caseId,
    ...(draft.requestId === undefined ? {} : { request_id: draft.requestId }),
    respondent: { name: draft.respondentName.trim(), team: draft.respondentTeam.trim() },
    answers: draft.answers.map(({ questionId, response }) => ({
      question_id: questionId,
      response: response.trim(),
    })),
    submitted_at: submittedAt,
  });
}

export interface DpiaOutcomeDraft {
  requestId: string;
  caseId?: string;
  decision: DpiaOutcome["decision"];
  reasons: string[];
  conditions: { action: string; owner: string; due: string }[];
  reviewDate: string;
  contact: string;
  decidedBy: string;
}

export function buildDpiaOutcomeArtifact(draft: DpiaOutcomeDraft, decidedAt: string): DpiaOutcome {
  return dpiaOutcomeSchema.parse({
    artifact: "dpia-outcome",
    request_id: draft.requestId,
    ...(draft.caseId === undefined ? {} : { case_id: draft.caseId }),
    decision: draft.decision,
    reasons: draft.reasons.map((reason) => reason.trim()).filter(Boolean),
    conditions: draft.conditions.map((condition) => ({
      action: condition.action.trim(),
      owner: condition.owner.trim(),
      due: condition.due.trim(),
    })),
    review_date: draft.reviewDate.trim(),
    contact: draft.contact.trim(),
    decided_by: draft.decidedBy.trim(),
    decided_at: decidedAt,
  });
}
