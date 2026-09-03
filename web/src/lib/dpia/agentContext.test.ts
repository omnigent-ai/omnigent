import { describe, expect, it } from "vitest";
import { createStudentSuccessAlertSeed } from "./seed";
import {
  buildDpiaAgentMessage,
  buildDpiaProcessingModelArtifact,
  officerMessageFromAgentMessage,
} from "./agentContext";

describe("DPIA agent context", () => {
  it("converts the browser case into the canonical processing-model artifact", () => {
    const caseData = createStudentSuccessAlertSeed();
    const artifact = buildDpiaProcessingModelArtifact(caseData);

    expect(artifact).toMatchObject({
      artifact: "processing-model",
      case_id: "student-success-alert",
      processing_model_version: 3,
    });
    expect(artifact.evidence).toHaveLength(caseData.evidence.length);
    expect(artifact.evidence.map(({ id }) => id)).toEqual(
      Array.from({ length: 15 }, (_, index) => `EV-${String(index + 1).padStart(2, "0")}`),
    );
    expect(artifact.lifecycle).toHaveLength(8);
    expect(artifact.facts.find(({ id }) => id === "hosting")).toMatchObject({
      field: "Model and database hosting",
      value: "",
      status: "missing",
      evidence_ids: [],
      processing_model_version: 3,
    });
    expect(artifact.facts.find(({ id }) => id === "security-controls")?.evidence_ids).toEqual([
      "EV-08",
      "EV-12",
    ]);
    expect(
      artifact.contradictions.find(({ id }) => id === "con-retention")?.source_evidence_ids,
    ).toEqual(["EV-05", "EV-06", "EV-15"]);
    expect(artifact.evidence_gaps).toContainEqual(
      expect.objectContaining({ id: "gap-hosting", material: true }),
    );
  });

  it("keeps the officer text separate from the authoritative JSON context", () => {
    const caseData = createStudentSuccessAlertSeed();
    const message = buildDpiaAgentMessage(caseData, "Correct the hosting fact.");

    expect(message).toContain("Officer message follows");
    expect(message).toContain("Correct the hosting fact.");
    expect(message).toContain('"artifact":"processing-model"');
    expect(message).toContain('"processing_model_version":3');
    expect(message).toContain('"finding_id":"det-vendor"');
    expect(officerMessageFromAgentMessage(message)).toBe("Correct the hosting fact.");
    expect(officerMessageFromAgentMessage("Ordinary question")).toBe("Ordinary question");
  });
});
