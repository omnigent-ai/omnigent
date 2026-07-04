import { describe, expect, it } from "vitest";
import { parseAskUserQuestionPreview } from "./askUserQuestion";

describe("parseAskUserQuestionPreview", () => {
  it("parses a single-question single-select payload", () => {
    // Missing ``description`` on each option defaults to "" so the
    // type stays strict (required field) without dropping the
    // option. The renderer hides empty descriptions conditionally.
    const preview =
      'AskUserQuestion({"questions": [{"question": "Pick a framework", ' +
      '"header": "Framework", "options": [{"label": "React"}, {"label": "Vue"}], ' +
      '"multiSelect": false}]})';
    const parsed = parseAskUserQuestionPreview(preview);
    expect(parsed).not.toBeNull();
    expect(parsed!.questions).toHaveLength(1);
    const q = parsed!.questions[0]!;
    expect(q.question).toBe("Pick a framework");
    expect(q.header).toBe("Framework");
    expect(q.multiSelect).toBe(false);
    expect(q.options).toEqual([
      { label: "React", description: "" },
      { label: "Vue", description: "" },
    ]);
  });

  it("defaults missing header to '' and preserves option preview when present", () => {
    // ``header`` is required on the typed shape but Claude may omit
    // it; default-to-"" keeps the question instead of dropping it.
    // ``preview`` is the new optional field for richer rendering;
    // strings ride through, anything else is silently dropped.
    const preview =
      'AskUserQuestion({"questions": [{"question": "Q", ' +
      '"options": [{"label": "A", "description": "first", "preview": "alpha preview"}, ' +
      '{"label": "B", "description": "second"}], ' +
      '"multiSelect": false}]})';
    const parsed = parseAskUserQuestionPreview(preview);
    expect(parsed).not.toBeNull();
    const q = parsed!.questions[0]!;
    expect(q.header).toBe("");
    expect(q.options[0]).toEqual({ label: "A", description: "first", preview: "alpha preview" });
    // No preview key when the input didn't carry one — keeps
    // downstream Object.keys / equality checks predictable.
    expect(q.options[1]).toEqual({ label: "B", description: "second" });
    expect("preview" in q.options[1]!).toBe(false);
  });

  it("preserves option descriptions and multiSelect across multi-question payloads", () => {
    // Mirrors the actual sample dump the user pasted from a live
    // Claude run: a three-question batch with mixed multiSelect
    // flags and rich descriptions on each option. The form needs
    // the description text to render the helper line under each
    // option label.
    const preview =
      'AskUserQuestion({"questions": [' +
      '{"question": "Which snacks should we stock for the demo?", ' +
      '"header": "Snacks", "options": [' +
      '{"label": "Popcorn", "description": "Light, crunchy, classic"}, ' +
      '{"label": "Chocolate", "description": "Sweet pick-me-up"}], ' +
      '"multiSelect": true}, ' +
      '{"question": "What time should the demo start?", ' +
      '"header": "Start time", "options": [' +
      '{"label": "Morning", "description": "9-11 AM"}, ' +
      '{"label": "Lunch", "description": "12-1 PM"}], ' +
      '"multiSelect": false}]})';
    const parsed = parseAskUserQuestionPreview(preview);
    expect(parsed).not.toBeNull();
    expect(parsed!.questions).toHaveLength(2);

    const snacks = parsed!.questions[0]!;
    expect(snacks.multiSelect).toBe(true);
    expect(snacks.options[0]).toEqual({
      label: "Popcorn",
      description: "Light, crunchy, classic",
    });

    const startTime = parsed!.questions[1]!;
    expect(startTime.multiSelect).toBe(false);
    expect(startTime.options[1]!.description).toBe("12-1 PM");
  });

  it("returns null for previews that don't match the AskUserQuestion shape", () => {
    // Other policy ASKs / PermissionRequests for non-AskUserQuestion
    // tools must NOT trigger the form rendering — the parser is the
    // only gate the UI has on which card flavor to show.
    expect(parseAskUserQuestionPreview('Bash({"command": "ls"})')).toBeNull();
    expect(parseAskUserQuestionPreview("just some text")).toBeNull();
    expect(parseAskUserQuestionPreview("AskUserQuestion(not json)")).toBeNull();
    expect(parseAskUserQuestionPreview("")).toBeNull();
  });

  it("returns null when the JSON parses but has no questions", () => {
    // A payload-shaped preview with empty `questions` would render
    // an empty form — drop instead.
    expect(parseAskUserQuestionPreview('AskUserQuestion({"questions": []})')).toBeNull();
    expect(parseAskUserQuestionPreview("AskUserQuestion({})")).toBeNull();
  });

  it("skips questions with empty options and returns null when nothing usable remains", () => {
    // The UI can't render a question with no options to pick from.
    // If EVERY question is unusable, the whole payload is dropped.
    expect(
      parseAskUserQuestionPreview(
        'AskUserQuestion({"questions": [{"question": "Q", "options": [], "multiSelect": false}]})',
      ),
    ).toBeNull();
  });
});
