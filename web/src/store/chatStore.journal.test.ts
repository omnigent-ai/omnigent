import { afterEach, describe, expect, it } from "vitest";
import type { JournalEntry } from "@/lib/sessionJournal";
import { handleSessionEvent, useChatStore } from "@/store/chatStore";
import { useSessionTasksActions } from "@/store/sessionTasksActions";
import { journalEntry, journalPlan } from "@/test/journalFixtures";

function appended(conversationId: string, entry: JournalEntry): void {
  handleSessionEvent({ type: "session_journal_appended", conversationId, entry }, conversationId);
}

afterEach(() => {
  useSessionTasksActions.setState({
    tasksTabToOpen: null,
    taskToShow: null,
    tasksTabAutoOpen: false,
  });
});

describe("a live journal entry", () => {
  it("opens the Tasks tab for the session's first plan, while that session is on screen", () => {
    useChatStore.setState({ conversationId: "conv" });
    const tabRequest = () => useSessionTasksActions.getState().tasksTabToOpen;
    appended(
      "conv",
      journalEntry(0, { kind: "status", step: "a", payload: { status: "started" } }),
    );
    expect(tabRequest()).toBeNull();
    // Another session's plan, and a later version of this one's, leave the tab alone.
    appended("other", journalPlan(0, ["a"]));
    appended("conv", journalPlan(1, ["a"], ["b"]));
    expect(tabRequest()).toBeNull();
    appended("conv", journalPlan(0, ["a"]));
    expect(useSessionTasksActions.getState()).toMatchObject({
      tasksTabToOpen: "conv",
      tasksTabAutoOpen: true,
    });
  });
});
