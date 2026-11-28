import { describe, expect, it } from "vitest";

import {
  pullRequestReviewerKey,
  type PullRequestReviewerTarget,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  EMPTY_PICKER_DRAFT,
  type PickerDraft,
  pickerDraftChange as draftChange,
  pickerDraftChecked as draftChecked,
  pickerDraftInScope as draftInScope,
  type PickerDraftScope,
  pickerDraftSummary,
  pickerDraftTargets as draftTargets,
  togglePickerDraft,
} from "./pickerDraft";

type ReviewerDraft = PickerDraft<PullRequestReviewerTarget>;
const EMPTY_REVIEWER_DRAFT: ReviewerDraft = EMPTY_PICKER_DRAFT;
const WORDS = { add: "to request", remove: "to withdraw", sends: "sent when you close" };
const draftSummary = (change: Parameters<typeof pickerDraftSummary>[0]) =>
  pickerDraftSummary(change, WORDS);
const toggleDraft = (
  draft: ReviewerDraft,
  prUrl: string,
  scope: PickerDraftScope,
  current: ReadonlySet<string>,
  target: PullRequestReviewerTarget,
) => togglePickerDraft(draft, prUrl, scope, current, pullRequestReviewerKey(target), target);

const PR = "https://github.com/acme/app/pull/1";
const OTHER = "https://github.com/acme/app/pull/2";
const alice = { login: "alice", kind: "user" as const };
const core = { login: "core", kind: "team" as const };
const requested = new Set(["team:core"]);
const SCOPE: PickerDraftScope = { owner: "user-1" };

const toggled = (...targets: (typeof alice | typeof core)[]): ReviewerDraft =>
  targets.reduce(
    (draft, target) => toggleDraft(draft, PR, SCOPE, requested, target),
    EMPTY_REVIEWER_DRAFT,
  );

describe("a picker draft, for reviewers", () => {
  it("checks a new reviewer and unchecks a requested one, as one change", () => {
    const draft = toggled(alice, core);
    expect([...draftChecked(requested, draft)]).toEqual(["user:alice"]);
    expect(draftChange(requested, draft)).toEqual({ prUrl: PR, add: [alice], remove: [core] });
  });

  it("leaves nothing to send once a reviewer is toggled back", () => {
    const draft = toggled(alice, alice, core, core);
    expect(draft.wanted.size).toBe(0);
    expect(draftChecked(requested, draft)).toBe(requested);
    expect(draftChange(requested, draft)).toBeNull();
  });

  it("sends only what still differs when the reviewers changed while it was open", () => {
    const draft = toggled(alice, core);
    // Someone else requested alice and withdrew core meanwhile.
    expect(draftChange(new Set(["user:alice"]), draft)).toBeNull();
  });

  it("starts over for another pull request", () => {
    const draft = toggleDraft(toggled(alice), OTHER, SCOPE, requested, core);
    expect(draft.prUrl).toBe(OTHER);
    expect(draftChange(requested, draft)).toEqual({ prUrl: OTHER, add: [], remove: [core] });
  });

  it("belongs only to the user it was checked as", () => {
    const draft = toggled(alice);
    expect(draftInScope(draft, { owner: "user-1" })).toBe(true);
    expect(draftInScope(draft, { owner: "user-2" })).toBe(false);
    expect(draftInScope(EMPTY_REVIEWER_DRAFT, SCOPE)).toBe(false);
  });

  it("starts over when checked as another user", () => {
    const asAlice = { owner: "user-2" };
    const draft = toggleDraft(toggled(alice), PR, asAlice, requested, core);
    expect(draft.scope).toBe(asAlice);
    expect(draftChange(requested, draft)).toEqual({ prUrl: PR, add: [], remove: [core] });
  });

  it("says what closing will send, and lists who it asks", () => {
    const both = toggled(alice, core);
    expect(draftSummary(draftChange(requested, both))).toBe(
      "1 to request, 1 to withdraw · sent when you close",
    );
    expect(draftSummary(draftChange(requested, toggled(core)))).toBe(
      "1 to withdraw · sent when you close",
    );
    expect(draftSummary(null)).toBeNull();
    expect(draftTargets(both)).toEqual([alice]);
  });

  it("sends nothing for an empty draft", () => {
    expect(draftChange(requested, EMPTY_REVIEWER_DRAFT)).toBeNull();
  });
});
