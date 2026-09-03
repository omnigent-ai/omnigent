import { describe, expect, it } from "vitest";
import {
  buildDpiaOutcomeArtifact,
  buildDpiaRequestArtifact,
  buildStakeholderResponseArtifact,
  dpiaOutcomeSchema,
  dpiaRequestSchema,
  latestArtifact,
  newDpiaRequestId,
  parseDpiaOutcomeText,
  parseDpiaRequestText,
  parseStakeholderResponseText,
  stakeholderResponseSchema,
} from "./requestArtifacts";

const requestDraft = {
  requesterName: "Priya Shah",
  requesterTeam: "Procurement",
  title: "Vendor Wellbeing Analytics",
  purpose: "Score student wellbeing survey responses to prioritise support outreach.",
  dataSubjects: "Enrolled students",
  personalData: "Survey responses, student ids",
  vendors: "Acme Analytics Ltd",
  timeline: "Pilot in October",
  knownUnknowns: ["Hosting location", "Subprocessor list"],
};

const validRequest = buildDpiaRequestArtifact(
  requestDraft,
  newDpiaRequestId(requestDraft.title, new Date("2026-08-22T09:00:00Z")),
  "2026-08-22T09:00:00.000Z",
);

const validResponse = buildStakeholderResponseArtifact(
  {
    caseId: "student-success-alert",
    requestId: validRequest.request_id,
    respondentName: "Jordan Ali",
    respondentTeam: "IT Security",
    answers: [
      { questionId: "q-hosting", response: "The model and database are hosted in London." },
    ],
  },
  "2026-08-22T10:00:00.000Z",
);

const validOutcome = buildDpiaOutcomeArtifact(
  {
    requestId: validRequest.request_id,
    caseId: "student-success-alert",
    decision: "approved-with-conditions",
    reasons: ["Screening indicates a full DPIA is likely before launch."],
    conditions: [
      {
        action: "Confirm the hosting region in the vendor contract.",
        owner: "Procurement",
        due: "2026-09-15",
      },
    ],
    reviewDate: "2027-02-01",
    contact: "privacy-office@university.example",
    decidedBy: "Alex Morgan",
  },
  "2026-08-22T12:00:00.000Z",
);

describe("dpia request artifact", () => {
  it("builds and parses a strict request", () => {
    expect(parseDpiaRequestText(JSON.stringify(validRequest))).toEqual(validRequest);
    expect(validRequest.request_id).toMatch(/^req-vendor-wellbeing-analyt/);
  });

  it("rejects extra fields, short fields, duplicate unknowns, and prose wrappers", () => {
    expect(() => dpiaRequestSchema.parse({ ...validRequest, auto_accept: true })).toThrow();
    expect(() =>
      dpiaRequestSchema.parse({
        ...validRequest,
        project: { ...validRequest.project, purpose: "short" },
      }),
    ).toThrow();
    expect(() =>
      dpiaRequestSchema.parse({ ...validRequest, known_unknowns: ["Hosting", "Hosting"] }),
    ).toThrow(/unique/i);
    expect(parseDpiaRequestText(`Request: ${JSON.stringify(validRequest)}`)).toBeNull();
  });
});

describe("stakeholder response artifact", () => {
  it("builds and parses a strict response", () => {
    expect(parseStakeholderResponseText(JSON.stringify(validResponse))).toEqual(validResponse);
  });

  it("allows omitting request_id for seeded cases", () => {
    const seededOnly = buildStakeholderResponseArtifact(
      {
        caseId: "student-success-alert",
        respondentName: "Jordan Ali",
        respondentTeam: "IT Security",
        answers: [{ questionId: "q-hosting", response: "Hosting is confirmed in London." }],
      },
      "2026-08-22T10:00:00.000Z",
    );
    expect(seededOnly.request_id).toBeUndefined();
  });

  it("rejects duplicate question ids, short answers, and extra fields", () => {
    expect(() =>
      stakeholderResponseSchema.parse({
        ...validResponse,
        answers: [...validResponse.answers, ...validResponse.answers],
      }),
    ).toThrow(/unique/i);
    expect(() =>
      stakeholderResponseSchema.parse({
        ...validResponse,
        answers: [{ question_id: "q-hosting", response: "short" }],
      }),
    ).toThrow();
    expect(() => stakeholderResponseSchema.parse({ ...validResponse, applied: true })).toThrow();
  });
});

describe("dpia outcome artifact", () => {
  it("builds and parses a strict outcome", () => {
    expect(parseDpiaOutcomeText(JSON.stringify(validOutcome))).toEqual(validOutcome);
  });

  it("requires conditions when approving with conditions", () => {
    expect(() => dpiaOutcomeSchema.parse({ ...validOutcome, conditions: [] })).toThrow(
      /condition/i,
    );
    expect(
      dpiaOutcomeSchema.parse({ ...validOutcome, decision: "approved", conditions: [] }).decision,
    ).toBe("approved");
  });
});

describe("latestArtifact", () => {
  it("returns the last parsable artifact in a transcript", () => {
    const older = { ...validRequest, submitted_at: "2026-08-22T08:00:00.000Z" };
    const texts = [
      "Hello, I need a DPIA.",
      JSON.stringify(older),
      "Officer note in between.",
      JSON.stringify(validRequest),
      "Trailing chatter.",
    ];
    expect(latestArtifact(texts, parseDpiaRequestText)).toEqual(validRequest);
    expect(latestArtifact(["no artifacts here"], parseDpiaRequestText)).toBeNull();
  });
});
