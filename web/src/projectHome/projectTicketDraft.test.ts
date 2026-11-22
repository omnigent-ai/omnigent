import { afterEach, describe, expect, it } from "vitest";
import { clearSessionDrafts, getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { restoreReplyDraft } from "@/lib/replyDraft";
import { addTicketToSessionDraft } from "./projectTicketDraft";

afterEach(clearSessionDrafts);

describe("project ticket draft context", () => {
  it("adds one removable exact ticket reference, preserving text, attachments and mentions", () => {
    const files = [new File(["notes"], "notes.txt")];
    const mentions = { files: [], sessions: [] };
    setSessionDraft("lead", { text: "Keep my question", files, mentions });
    const ticket = { id: "ticket-a", project_id: "project", title: "Ship the board" };
    addTicketToSessionDraft("lead", ticket);
    addTicketToSessionDraft("lead", { ...ticket, title: "Updated title" });
    const draft = getSessionDraft("lead")!;
    const restored = restoreReplyDraft(draft.text, draft.replyDraft);
    expect(restored.quotes).toHaveLength(1);
    expect(restored.quotes[0]).toMatchObject({
      before: "Keep my question",
      text: "Ship the board\nTicket ID: ticket-a\nProject ID: project",
    });
    expect(draft.files).toEqual(files);
    expect(draft.mentions).toBe(mentions);
    expect(getSessionDraft("worker")).toBeUndefined();
  });
});
