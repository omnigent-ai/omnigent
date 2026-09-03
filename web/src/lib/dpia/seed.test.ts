import { beforeEach, describe, expect, it } from "vitest";
import { calculateReadiness } from "./readiness";
import { createStudentSuccessAlertSeed, readinessDefinitions } from "./seed";
import { dpiaCaseSnapshotSchema } from "./schemas";
import { SynthesisRefusal, synthesizeDecisionPack } from "./synthesis";
import {
  answerStakeholderQuestion,
  bindDpiaCaseSession,
  dpiaStorageKey,
  loadDpiaCase,
  recordOfficerDecision,
  replayValidatedInvestigation,
  saveDpiaCase,
  updateDpiaIntake,
} from "./dpiaApi";

beforeEach(() => {
  localStorage.clear();
});

function acceptedCase() {
  const caseData = createStudentSuccessAlertSeed();
  return recordOfficerDecision(caseData, {
    action: "accepted",
    outcome: "full-dpia-likely",
    rationale:
      "The evidence supports progressing to a full DPIA while identified gaps are resolved.",
    officer: "Alex Morgan",
    decidedAt: "2026-08-21T12:00:00Z",
    processingModelVersion: caseData.processingModel.version,
    policyPackVersion: caseData.policyPack.version,
  });
}

function expectSynthesisRefusal(
  candidate: ReturnType<typeof createStudentSuccessAlertSeed>,
  code: SynthesisRefusal["code"],
) {
  try {
    synthesizeDecisionPack(candidate, "2026-08-21T12:05:00Z");
    throw new Error(`Expected synthesis refusal: ${code}`);
  } catch (error) {
    expect(error).toBeInstanceOf(SynthesisRefusal);
    expect((error as SynthesisRefusal).code).toBe(code);
  }
}

describe("validated DPIA snapshot", () => {
  it("persists an optional case session without changing the processing-model version", () => {
    const caseData = createStudentSuccessAlertSeed();
    const bound = bindDpiaCaseSession(caseData, "conv_dpia", "2026-08-21T12:10:00Z", "Alex Morgan");

    expect(bound).toMatchObject({ sessionId: "conv_dpia", processingModel: { version: 3 } });
    expect(bound.audit.at(-1)).toMatchObject({
      action: "Connected case agent",
      newValue: "conv_dpia",
    });
  });

  it("is schema-valid, synthetic, and starts at five of eight answerable dimensions", () => {
    const caseData = createStudentSuccessAlertSeed();
    const parsed = dpiaCaseSnapshotSchema.parse(caseData);
    const readiness = calculateReadiness(
      parsed.processingModel,
      parsed.evidence,
      parsed.contradictions,
      readinessDefinitions,
      parsed.determinations,
    );

    expect(readiness).toMatchObject({ answerable: 5, total: 8 });
    expect(parsed.lifecycle).toHaveLength(8);
    expect(parsed.evidence).toHaveLength(15);
    expect(parsed.evidence.every((item) => item.synthetic)).toBe(true);
    expect(parsed.questions.length).toBeGreaterThanOrEqual(6);
    expect(parsed.risks).toHaveLength(8);
  });

  it("persists a schema-valid case and recovers safely from corrupt storage", () => {
    const caseData = acceptedCase();
    saveDpiaCase(caseData);

    expect(loadDpiaCase(caseData.id)).toMatchObject({
      source: "persisted",
      recoveredInvalidState: false,
      caseData: { officerDecision: { action: "accepted" } },
    });

    localStorage.setItem(dpiaStorageKey(caseData.id), "{not valid json");
    const recovered = loadDpiaCase(caseData.id);
    expect(recovered).toMatchObject({
      source: "seed",
      recoveredInvalidState: true,
      caseData: { snapshotLabel: "Validated demo snapshot" },
    });
    expect(recovered.caseData.officerDecision).toBeUndefined();
  });

  it("requires rationale for an edited or rejected officer determination", () => {
    const caseData = createStudentSuccessAlertSeed();
    expect(() =>
      recordOfficerDecision(caseData, {
        action: "rejected",
        outcome: "no-full-dpia-indicated",
        rationale: "No",
        officer: "Alex Morgan",
        decidedAt: "2026-08-21T12:00:00Z",
        processingModelVersion: 3,
        policyPackVersion: caseData.policyPack.version,
      }),
    ).toThrow(/substantive rationale/);
  });

  it("versions material intake edits, resolves cited contradictions, and makes dependencies stale", () => {
    const caseData = createStudentSuccessAlertSeed();
    const updated = updateDpiaIntake(
      caseData,
      {
        "intended-outcome":
          "Persistent high-risk scores can trigger attendance escalation or fitness-to-study referral.",
      },
      "2026-08-21T12:15:00Z",
      "Alex Morgan",
    );

    expect(updated.processingModel.version).toBe(4);
    expect(updated.contradictions.find(({ id }) => id === "con-significant-effect")).toMatchObject({
      resolved: true,
    });
    expect(
      updated.determinations.filter(({ status }) => status === "stale-after-change"),
    ).not.toHaveLength(0);
    expect(
      updated.determinations
        .filter(({ status }) => status !== "stale-after-change")
        .every(({ processingModelVersion }) => processingModelVersion === 4),
    ).toBe(true);
    expect(updated.audit.at(-1)).toMatchObject({
      actor: "Alex Morgan",
      newValue: "Processing model v4",
    });
  });

  it("stales the union of dependencies once when multiple material facts change", () => {
    const updated = updateDpiaIntake(
      createStudentSuccessAlertSeed(),
      {
        hosting: "The model and primary database are hosted in London.",
        "access-controls": "SSO, MFA, role-based access, and export logging are required.",
      },
      "2026-08-21T12:17:00Z",
      "Alex Morgan",
    );

    expect(updated.processingModel.version).toBe(4);
    expect(
      updated.determinations
        .filter(({ status }) => status === "stale-after-change")
        .map(({ id }) => id)
        .sort(),
    ).toEqual(["det-security", "det-vendor"]);
    expect(
      updated.determinations
        .filter(({ status }) => status !== "stale-after-change")
        .every(({ processingModelVersion }) => processingModelVersion === 4),
    ).toBe(true);
  });

  it("preserves earlier stale findings across consecutive model versions", () => {
    const versionFour = updateDpiaIntake(
      createStudentSuccessAlertSeed(),
      { hosting: "The model and primary database are hosted in London." },
      "2026-08-21T12:18:00Z",
      "Alex Morgan",
    );
    const versionFive = updateDpiaIntake(
      versionFour,
      { "access-controls": "SSO, MFA, role-based access, and export logging are required." },
      "2026-08-21T12:19:00Z",
      "Alex Morgan",
    );

    expect(versionFive.processingModel.version).toBe(5);
    expect(
      versionFive.determinations
        .filter(({ status }) => status === "stale-after-change")
        .map(({ id, processingModelVersion }) => ({ id, processingModelVersion })),
    ).toEqual([
      { id: "det-vendor", processingModelVersion: 3 },
      { id: "det-security", processingModelVersion: 4 },
    ]);
    expect(
      versionFive.determinations
        .filter(({ status }) => status !== "stale-after-change")
        .every(({ processingModelVersion }) => processingModelVersion === 5),
    ).toBe(true);
  });

  it("records attributed answers as synthetic evidence and invalidates dependent findings", () => {
    const caseData = acceptedCase();
    const updated = answerStakeholderQuestion(caseData, {
      questionId: "q-hosting",
      response: "The primary database and model are hosted in London, with backups in Cardiff.",
      answeredBy: "Chloe Martin",
      answeredAt: "2026-08-21T12:20:00Z",
    });

    expect(updated.processingModel).toMatchObject({ version: 4 });
    expect(updated.officerDecision).toBeUndefined();
    expect(updated.processingModel.facts.find(({ id }) => id === "hosting")).toMatchObject({
      status: "confirmed",
      value: "The primary database and model are hosted in London, with backups in Cardiff.",
    });
    expect(updated.questions.find(({ id }) => id === "q-hosting")).toMatchObject({
      status: "answered",
      answeredBy: "Chloe Martin",
    });
    expect(updated.evidence.at(-1)).toMatchObject({ synthetic: true, owner: "Chloe Martin" });
    expect(updated.determinations.find(({ id }) => id === "det-vendor")).toMatchObject({
      status: "stale-after-change",
    });
    expect(
      updated.determinations
        .filter(({ status }) => status === "stale-after-change")
        .map(({ id }) => id),
    ).toEqual(["det-vendor"]);
    expect(
      updated.determinations
        .filter(({ status }) => status !== "stale-after-change")
        .every(({ processingModelVersion }) => processingModelVersion === 4),
    ).toBe(true);
    expect(updated.audit.at(-1)).toMatchObject({
      actor: "Chloe Martin",
      action: "Answered targeted question",
      priorValue: "Processing model v3",
      newValue: expect.stringContaining("Processing model v4"),
    });
    expect(updated.audit.at(-1)?.newValue).toContain(
      "The primary database and model are hosted in London",
    );
  });

  it("uses one attributed response as evidence for every fact blocked by a question", () => {
    const response =
      "Northstar Analytics and Contoso Support receive case data in the UK; no access from other countries is permitted.";
    const updated = answerStakeholderQuestion(createStudentSuccessAlertSeed(), {
      questionId: "q-subprocessors",
      response,
      answeredBy: "Chloe Martin",
      answeredAt: "2026-08-21T12:20:30Z",
    });

    expect(updated.processingModel.version).toBe(4);
    expect(
      updated.processingModel.facts
        .filter(({ id }) => id === "subprocessors" || id === "international-access")
        .map(({ id, value }) => ({ id, value })),
    ).toEqual([
      { id: "subprocessors", value: response },
      { id: "international-access", value: response },
    ]);
    expect(
      updated.determinations
        .filter(({ status }) => status === "stale-after-change")
        .map(({ id }) => id),
    ).toEqual(["det-vendor"]);
  });

  it("answers a non-material question without versioning or clearing the officer decision", () => {
    const decided = acceptedCase();
    const nonMaterial = dpiaCaseSnapshotSchema.parse({
      ...decided,
      processingModel: {
        ...decided.processingModel,
        facts: decided.processingModel.facts.map((fact) =>
          fact.id === "hosting" ? { ...fact, material: false } : fact,
        ),
      },
    });
    const updated = answerStakeholderQuestion(nonMaterial, {
      questionId: "q-hosting",
      response: "The primary database and model are hosted in London.",
      answeredBy: "Chloe Martin",
      answeredAt: "2026-08-21T12:21:00Z",
    });

    expect(updated.processingModel.version).toBe(3);
    expect(updated.officerDecision).toEqual(nonMaterial.officerDecision);
    expect(updated.determinations.some(({ status }) => status === "stale-after-change")).toBe(
      false,
    );
    expect(
      updated.determinations.every(({ processingModelVersion }) => processingModelVersion === 3),
    ).toBe(true);
    expect(updated.audit.at(-1)).toMatchObject({
      action: "Answered targeted question",
      priorValue: "Processing model v3",
      newValue: expect.stringContaining("Processing model v3"),
    });
  });

  it("replays reviewed role outputs against the current model version without claiming a live run", () => {
    const changed = updateDpiaIntake(
      createStudentSuccessAlertSeed(),
      {
        "intended-outcome":
          "Persistent high-risk scores can trigger attendance escalation or fitness-to-study referral.",
      },
      "2026-08-21T12:15:00Z",
      "Alex Morgan",
    );
    const replayed = replayValidatedInvestigation(changed, "2026-08-21T12:30:00Z");

    expect(
      replayed.determinations.every(({ processingModelVersion }) => processingModelVersion === 4),
    ).toBe(true);
    expect(replayed.determinations.some(({ status }) => status === "stale-after-change")).toBe(
      false,
    );
    expect(replayed.audit.at(-1)).toMatchObject({
      action: "Replayed validated investigation",
      newValue: expect.stringContaining("no live agent run"),
    });
    expect(replayed.verification.notes.at(-1)).toContain("live agent output was not used");
  });
});

describe("decision-pack synthesis", () => {
  it("assembles only schema-valid, officer-approved, same-version inputs", () => {
    const pack = synthesizeDecisionPack(acceptedCase(), "2026-08-21T12:05:00Z");

    expect(pack).toMatchObject({
      caseId: "student-success-alert",
      processingModelVersion: 3,
      recommendation: "full-dpia-likely",
      officerDecision: { officer: "Alex Morgan" },
    });
    expect(pack.risks).toHaveLength(8);
  });

  it("refuses missing, stale, unsupported, and cross-version inputs", () => {
    const missingDecision = createStudentSuccessAlertSeed();
    expectSynthesisRefusal(missingDecision, "missing-officer-decision");

    const stale = acceptedCase();
    stale.determinations[0].status = "stale-after-change";
    expectSynthesisRefusal(stale, "stale-input");

    const unsupported = acceptedCase();
    unsupported.determinations[0].evidenceReferences = [];
    expectSynthesisRefusal(unsupported, "unsupported-finding");

    const crossVersion = acceptedCase();
    crossVersion.determinations[0].processingModelVersion = 2;
    expectSynthesisRefusal(crossVersion, "cross-version-input");
  });
});
