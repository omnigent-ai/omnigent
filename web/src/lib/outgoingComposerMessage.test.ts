import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { SESSION_MENTION_HINT } from "@/lib/composerMentions";
import { formatLocalReviewComments } from "@/lib/localReviewComments";
import { buildOutgoingMessage, carriesMessageContext } from "./outgoingComposerMessage";

const file = { token: "@a.ts", marker: "[Attached: a.ts]" };
const session = { token: "@auth", marker: '[Session: "auth" id=s1 subagent=no]' };
const comment = { id: "c1", body: "Rename this" } as unknown as Comment;
const none = { mentions: [], reviewComments: [] };

describe("buildOutgoingMessage", () => {
  it("sends plain text unchanged", () => {
    expect(buildOutgoingMessage({ text: "hi", quotes: [] }, "hi", none)).toEqual({ text: "hi" });
  });

  it("expands tags, appends review comments and one session hint", () => {
    const { text } = buildOutgoingMessage(
      { text: "Ask @auth about @a.ts", quotes: [] },
      "Ask @auth about @a.ts",
      {
        mentions: [file, session],
        reviewComments: [comment],
      },
    );
    expect(text).toBe(
      `Ask ${session.marker} about ${file.marker}\n\n` +
        `${formatLocalReviewComments([comment], { includeInstruction: false })}\n\n${SESSION_MENTION_HINT}`,
    );
  });

  it("asks to address review comments when nothing is authored", () => {
    const { text } = buildOutgoingMessage({ text: "", quotes: [] }, "", {
      mentions: [],
      reviewComments: [comment],
    });
    expect(text).toBe(formatLocalReviewComments([comment]));
  });

  it("expands tags in quote preambles and keeps the quote structure", () => {
    const draft = {
      text: "then @a.ts",
      quotes: [{ id: "q", before: "per @auth", text: "quoted" }],
    };
    const outgoing = buildOutgoingMessage(draft, "ignored with quotes", {
      mentions: [file, session],
      reviewComments: [],
    });
    expect(outgoing.replyDraft).toEqual({
      version: 1,
      quotes: [{ before: `per ${session.marker}`, text: "quoted" }],
      text: `then ${file.marker}\n\n${SESSION_MENTION_HINT}`,
    });
    expect(outgoing.text).toContain("> quoted");
  });
});

describe("carriesMessageContext", () => {
  const bare = { text: "/review", quotes: [] };
  it.each([
    ["nothing extra", bare, [], none, false],
    ["a quote", { ...bare, quotes: [{ id: "q", before: "", text: "x" }] }, [], none, true],
    ["a file", bare, [new File(["x"], "x.txt")], none, true],
    ["a tag", bare, [], { mentions: [file], reviewComments: [] }, true],
    ["a review comment", bare, [], { mentions: [], reviewComments: [comment] }, true],
  ])("with %s: %s", (_label, draft, files, context, expected) => {
    expect(carriesMessageContext(draft, files, context)).toBe(expected);
  });
});
