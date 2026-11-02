import { describe, expect, it } from "vitest";
import { addRole, sdlcTeam, nativeTeam } from "../../shared/team.ts";
import { type MentionTarget, handleFor, insertMention, mentionAtCaret, mentionedTargets, roleTargets, sessionTargets, suggestMentions } from "./mentions.ts";

const targets: MentionTarget[] = [
  { handle: "PM", label: "PM", sessionId: "s_pm" },
  { handle: "QA", label: "QA", sessionId: "s_qa" },
  { handle: "Reviewer", label: "Reviewing Developer", sessionId: null },
];

describe("mentionedTargets", () => {
  it("finds each mentioned target once, case-insensitively, ignoring emails and unknown handles", () => {
    expect(mentionedTargets("@pm and @QA, then @PM again", targets).map((target) => target.handle)).toEqual(["PM", "QA"]);
    expect(mentionedTargets("mail po@pm.example or @nobody", targets)).toEqual([]);
    expect(mentionedTargets("(@Reviewer) please", targets).map((target) => target.handle)).toEqual(["Reviewer"]);
  });
});

describe("mentionAtCaret and insertMention", () => {
  it("completes the @word being typed and keeps the rest of the message", () => {
    const text = "hey @q how are you";
    const caret = "hey @q".length;
    expect(mentionAtCaret(text, caret)).toEqual({ start: 4, query: "q" });
    expect(insertMention(text, 4, caret, "QA")).toEqual({ text: "hey @QA how are you", caret: 8 });
    expect(mentionAtCaret("an email a@b", 12)).toBeNull();
    expect(mentionAtCaret("@", 1)).toEqual({ start: 0, query: "" });
  });
});

describe("suggestMentions", () => {
  it("puts handle matches before label matches", () => {
    expect(suggestMentions("", targets)).toHaveLength(3);
    expect(suggestMentions("re", targets).map((target) => target.handle)).toEqual(["Reviewer"]);
    expect(suggestMentions("dev", targets).map((target) => target.handle)).toEqual(["Reviewer"]);
  });
});

describe("handleFor", () => {
  it("makes a short mentionable handle from a session name", () => {
    expect(handleFor("Fix the build!")).toBe("fix-the-build");
    expect(handleFor("42 tasks")).toBe("s-42-tasks");
  });
});

describe("room targets", () => {
  it("addresses every SDLC role by a short handle, through its current session", () => {
    const roles = roleTargets(sdlcTeam(), (role) => (role === "pm" ? "s_pm" : null));
    expect(roles.map((target) => target.handle)).toEqual(["PM", "Architect", "Developer", "Reviewer", "QA", "DevOps"]);
    expect(roles.find((target) => target.handle === "Reviewer")).toMatchObject({ label: "Reviewing Developer", sessionId: null });
    expect(roles[0].sessionId).toBe("s_pm");
    // A custom role is addressed by its title without spaces.
    expect(roleTargets(addRole(sdlcTeam(), "UX Designer").team, () => null).at(-1)).toMatchObject({ handle: "UXDesigner", label: "UX Designer" });
    // The native team's renamed PM goes by its abbreviation.
    expect(roleTargets(nativeTeam(), () => "s_pm")).toEqual([{ handle: "FSD", label: "Full Stack Developer (FSD)", sessionId: "s_pm" }]);
  });

  it("gives sessions with the same name distinct handles", () => {
    expect(sessionTargets([{ id: "a", name: "Claude" }, { id: "b", name: "claude" }]).map((target) => target.handle)).toEqual(["claude", "claude-2"]);
  });
});
