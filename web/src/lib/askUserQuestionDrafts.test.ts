import { beforeEach, describe, expect, it } from "vitest";
import { parseAskUserQuestionPreview, type ClaudeQuestion } from "./askUserQuestion";
import {
  type AskUserQuestionDraft,
  askUserQuestionSignature,
  clearAskUserQuestionDraft,
  getAskUserQuestionDraft,
  saveAskUserQuestionDraft,
} from "./askUserQuestionDrafts";

const questions: ClaudeQuestion[] = [
  { question: "Pick a region", options: [], multiSelect: false },
];
const signature = askUserQuestionSignature(questions);
const draft: AskUserQuestionDraft = {
  currentIndex: 0,
  selections: { "Pick a region": "" },
  customSelected: { "Pick a region": true },
  customInputs: { "Pick a region": "eu-west" },
};

describe("askUserQuestionDrafts", () => {
  beforeEach(() => {
    for (let index = 0; index <= 20; index++) clearAskUserQuestionDraft(`elic_${index}`);
  });

  it("matches the structured payload and the parsed preview of the same questions", () => {
    const structured = [
      {
        multiSelect: false,
        options: [{ label: "eu-west", preview: "Ireland" }],
        question: "Pick a region",
      },
    ] as ClaudeQuestion[];
    const parsed = parseAskUserQuestionPreview(
      'AskUserQuestion({"questions": [{"question": "Pick a region", ' +
        '"options": [{"label": "eu-west"}], "multiSelect": false}]})',
    )!.questions;

    expect(askUserQuestionSignature(structured)).toBe(askUserQuestionSignature(parsed));
    expect(askUserQuestionSignature(structured)).not.toBe(
      askUserQuestionSignature([{ ...structured[0]!, multiSelect: true }]),
    );
  });

  it("restores a copy, and only for the questions it was saved with", () => {
    saveAskUserQuestionDraft("elic_0", signature, draft);

    getAskUserQuestionDraft("elic_0", signature)!.customInputs["Pick a region"] = "changed";
    expect(getAskUserQuestionDraft("elic_0", signature)).toEqual(draft);
    const otherSignature = askUserQuestionSignature([
      { ...questions[0]!, question: "Pick a zone" },
    ]);
    expect(getAskUserQuestionDraft("elic_0", otherSignature)).toBeUndefined();

    clearAskUserQuestionDraft("elic_0", otherSignature);
    expect(getAskUserQuestionDraft("elic_0", signature)).toEqual(draft);
    clearAskUserQuestionDraft("elic_0", signature);
    expect(getAskUserQuestionDraft("elic_0", signature)).toBeUndefined();
  });

  it("keeps only the 20 most recently saved drafts", () => {
    for (let index = 0; index < 20; index++) {
      saveAskUserQuestionDraft(`elic_${index}`, signature, draft);
    }
    saveAskUserQuestionDraft("elic_0", signature, draft);
    saveAskUserQuestionDraft("elic_20", signature, draft);

    expect(getAskUserQuestionDraft("elic_1", signature)).toBeUndefined();
    expect(getAskUserQuestionDraft("elic_0", signature)).toEqual(draft);
    expect(getAskUserQuestionDraft("elic_2", signature)).toEqual(draft);
    expect(getAskUserQuestionDraft("elic_20", signature)).toEqual(draft);
  });
});
