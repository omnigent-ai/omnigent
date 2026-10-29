import { beforeEach, describe, expect, it } from "vitest";
import type { ClaudeQuestion } from "./askUserQuestion";
import {
  type AskUserQuestionDraft,
  clearAskUserQuestionDraft,
  getAskUserQuestionDraft,
  saveAskUserQuestionDraft,
} from "./askUserQuestionDrafts";

const questions: ClaudeQuestion[] = [
  { question: "Pick a region", options: [], multiSelect: false },
];
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

  it("restores a draft only for the questions it was saved with", () => {
    saveAskUserQuestionDraft("elic_0", questions, draft);

    expect(getAskUserQuestionDraft("elic_0", structuredClone(questions))).toEqual(draft);
    getAskUserQuestionDraft("elic_0", questions)!.customInputs["Pick a region"] = "changed";
    expect(getAskUserQuestionDraft("elic_0", questions)?.customInputs).toEqual({
      "Pick a region": "eu-west",
    });
    expect(
      getAskUserQuestionDraft("elic_0", [{ ...questions[0]!, question: "Pick a zone" }]),
    ).toBeUndefined();
  });

  it("keeps only the 20 most recently saved drafts", () => {
    for (let index = 0; index < 20; index++) {
      saveAskUserQuestionDraft(`elic_${index}`, questions, draft);
    }
    saveAskUserQuestionDraft("elic_0", questions, draft);
    saveAskUserQuestionDraft("elic_20", questions, draft);

    expect(getAskUserQuestionDraft("elic_1", questions)).toBeUndefined();
    expect(getAskUserQuestionDraft("elic_0", questions)).toEqual(draft);
    expect(getAskUserQuestionDraft("elic_2", questions)).toEqual(draft);
    expect(getAskUserQuestionDraft("elic_20", questions)).toEqual(draft);
  });
});
