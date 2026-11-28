import { describe, expect, it } from "vitest";
import { quoteMarkdown, withQuotedReply } from "./commentQuote";

describe("quoting a comment into a reply", () => {
  it("quotes every line, with a bare marker for blank ones", () => {
    expect(quoteMarkdown("First\n\nSecond")).toBe("> First\n>\n> Second");
  });

  it("reads Windows and old Mac line endings as line breaks, and drops trailing ones", () => {
    expect(quoteMarkdown("One\r\nTwo\rThree\n\n")).toBe("> One\n> Two\n> Three");
  });

  it("starts an empty draft with the quote and a blank line for the reply", () => {
    expect(withQuotedReply("", "Looks good")).toBe("> Looks good\n\n");
    expect(withQuotedReply("  \n", "Looks good")).toBe("> Looks good\n\n");
  });

  it("adds the quote after what's written, a blank line apart, and never replaces it", () => {
    expect(withQuotedReply("I agree.\n", "Looks good")).toBe("I agree.\n\n> Looks good\n\n");
  });

  it("keeps a second quote apart from the first", () => {
    const once = withQuotedReply("", "First");
    expect(withQuotedReply(once, "Second")).toBe("> First\n\n> Second\n\n");
  });

  it("keeps an upload still under way in the draft", () => {
    const uploading = "See ![Uploading shot.png…]()";
    expect(withQuotedReply(uploading, "Why?")).toBe(`${uploading}\n\n> Why?\n\n`);
  });
});
