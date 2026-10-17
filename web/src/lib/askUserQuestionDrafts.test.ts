import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AskUserQuestionDraft } from "./askUserQuestionDrafts";

const key = "omnigent.askUserQuestionDrafts";

function draft(overrides: Partial<AskUserQuestionDraft> = {}): AskUserQuestionDraft {
  return {
    currentIndex: 1,
    selections: { first: "A", second: [] },
    customSelected: { first: false, second: true },
    customInputs: { first: "", second: "typed" },
    ...overrides,
  };
}

describe("AskUserQuestion drafts", () => {
  beforeEach(() => {
    vi.resetModules();
    sessionStorage.clear();
  });
  afterEach(() => sessionStorage.clear());

  it("restores a saved draft after a reload", async () => {
    const saved = draft();
    const first = await import("./askUserQuestionDrafts");
    first.setAskUserQuestionDraft("elic_1", saved);
    expect(first.getAskUserQuestionDraft("elic_1")).toEqual(saved);
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({ elic_1: saved });

    vi.resetModules();
    const reloaded = await import("./askUserQuestionDrafts");
    expect(reloaded.getAskUserQuestionDraft("elic_1")).toEqual(saved);
    expect(reloaded.getAskUserQuestionDraft("elic_2")).toBeUndefined();
  });

  it("drops a draft once it is back at the form's defaults", async () => {
    const { getAskUserQuestionDraft, setAskUserQuestionDraft } =
      await import("./askUserQuestionDrafts");
    setAskUserQuestionDraft("elic_1", draft());
    setAskUserQuestionDraft("elic_1", {
      currentIndex: 0,
      selections: { first: "", second: [] },
      customSelected: { first: false, second: false },
      customInputs: { first: "", second: "" },
    });
    expect(getAskUserQuestionDraft("elic_1")).toBeUndefined();
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it("clears only the answered question's draft", async () => {
    const { clearAskUserQuestionDraft, getAskUserQuestionDraft, setAskUserQuestionDraft } =
      await import("./askUserQuestionDrafts");
    setAskUserQuestionDraft("elic_1", draft());
    setAskUserQuestionDraft("elic_2", draft());
    clearAskUserQuestionDraft("elic_1");
    expect(getAskUserQuestionDraft("elic_1")).toBeUndefined();
    expect(Object.keys(JSON.parse(sessionStorage.getItem(key)!))).toEqual(["elic_2"]);
  });

  it("keeps secret answers in memory only", async () => {
    const { getAskUserQuestionDraft, setAskUserQuestionDraft } =
      await import("./askUserQuestionDrafts");
    const saved = draft({ customInputs: { first: "", second: "hunter2" } });
    setAskUserQuestionDraft("elic_1", saved, ["second"]);
    expect(getAskUserQuestionDraft("elic_1")).toEqual(saved);
    expect(JSON.parse(sessionStorage.getItem(key)!).elic_1.customInputs).toEqual({ first: "" });
  });

  it("ignores stored entries that do not match the draft shape", async () => {
    const as = <T>(value: unknown) => value as T;
    sessionStorage.setItem(
      key,
      JSON.stringify({
        valid: draft(),
        negativeIndex: draft({ currentIndex: -1 }),
        fractionalIndex: draft({ currentIndex: 1.5 }),
        badSelection: draft({ selections: as({ first: 42 }) }),
        badFlag: draft({ customSelected: as({ first: "yes" }) }),
        badText: draft({ customInputs: as({ first: null }) }),
        notAnObject: "draft",
      }),
    );
    const { getAskUserQuestionDraft } = await import("./askUserQuestionDrafts");
    expect(getAskUserQuestionDraft("valid")).toEqual(draft());
    for (const id of [
      "negativeIndex",
      "fractionalIndex",
      "badSelection",
      "badFlag",
      "badText",
      "notAnObject",
    ]) {
      expect(getAskUserQuestionDraft(id), id).toBeUndefined();
    }
  });

  it("drops a draft whose inner map carries a prototype-polluting key", async () => {
    // JSON.parse keeps __proto__/constructor as own keys; copying them onto a
    // plain-object map would taint its prototype, so the draft counts as corrupt.
    sessionStorage.setItem(
      key,
      '{"pollutedProto":{"currentIndex":0,"selections":{"__proto__":["A"]},"customSelected":{},"customInputs":{}},' +
        '"pollutedCtor":{"currentIndex":0,"selections":{},"customSelected":{"constructor":true},"customInputs":{}},' +
        '"good":{"currentIndex":0,"selections":{"first":"A"},"customSelected":{},"customInputs":{}}}',
    );
    const { getAskUserQuestionDraft } = await import("./askUserQuestionDrafts");
    expect(getAskUserQuestionDraft("pollutedProto")).toBeUndefined();
    expect(getAskUserQuestionDraft("pollutedCtor")).toBeUndefined();
    expect(getAskUserQuestionDraft("good")).toBeDefined();
  });

  it("writes a top-level __proto__ id without polluting the prototype", async () => {
    const { getAskUserQuestionDraft, setAskUserQuestionDraft } =
      await import("./askUserQuestionDrafts");
    setAskUserQuestionDraft("__proto__", draft());
    expect(getAskUserQuestionDraft("__proto__")).toEqual(draft());
    // The serialized root must keep the id as an own key, not a prototype.
    const stored = JSON.parse(sessionStorage.getItem(key)!) as Record<string, unknown>;
    expect(Object.hasOwn(stored, "__proto__")).toBe(true);
    expect(({} as Record<string, unknown>).currentIndex).toBeUndefined();
  });

  it.each(["not json", "null", "[]", "42"])("ignores an invalid storage root: %s", async (raw) => {
    sessionStorage.setItem(key, raw);
    const { getAskUserQuestionDraft } = await import("./askUserQuestionDrafts");
    expect(getAskUserQuestionDraft("elic_1")).toBeUndefined();
  });

  it("evicts the least recently updated drafts beyond the cap", async () => {
    const { getAskUserQuestionDraft, setAskUserQuestionDraft } =
      await import("./askUserQuestionDrafts");
    for (let i = 0; i <= 20; i++) setAskUserQuestionDraft(`elic_${i}`, draft());
    expect(getAskUserQuestionDraft("elic_0")).toBeUndefined();
    expect(getAskUserQuestionDraft("elic_1")).toBeDefined();
    expect(getAskUserQuestionDraft("elic_20")).toBeDefined();

    // Editing an old draft makes it recent again, so another one goes first.
    setAskUserQuestionDraft("elic_1", draft({ currentIndex: 0 }));
    setAskUserQuestionDraft("elic_21", draft());
    expect(getAskUserQuestionDraft("elic_1")).toBeDefined();
    expect(getAskUserQuestionDraft("elic_2")).toBeUndefined();
  });

  it("keeps a reference-counted mark in flight until every overlap clears", async () => {
    const { markApprovalInFlight, clearApprovalInFlight, isApprovalInFlight } =
      await import("./askUserQuestionDrafts");
    // Chat and Inbox can each submit the same id; the mark must outlast the
    // first clear and drop only once the matching clear count reaches zero.
    markApprovalInFlight("elic_overlap");
    markApprovalInFlight("elic_overlap");
    clearApprovalInFlight("elic_overlap");
    expect(isApprovalInFlight("elic_overlap")).toBe(true);
    clearApprovalInFlight("elic_overlap");
    expect(isApprovalInFlight("elic_overlap")).toBe(false);
  });
});
