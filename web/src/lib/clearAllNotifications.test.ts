import { createElement } from "react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { getSeenCommentIds } from "@/hooks/useSeenComments";
import {
  isConversationUnseen,
  markConversationUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";
import { clearAllNotifications } from "./clearAllNotifications";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(async () => new Response(null, { status: 204 })),
}));

function conversation(id: string, pendingApprovals = 0): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1_000,
    updated_at: 2_000,
    labels: {},
    permission_level: 4,
    status: "idle",
    pending_elicitations_count: pendingApprovals,
  };
}

function activeToastIds(): (string | number)[] {
  return toast.getToasts().map((active) => active.id);
}

beforeEach(() => {
  localStorage.clear();
  resetReadStateForTests();
});

afterEach(() => {
  for (const id of activeToastIds()) toast.dismiss(id);
});

describe("clearAllNotifications", () => {
  it("marks unread sessions and comments read, dismisses toasts, and preserves approvals", () => {
    const unread = conversation("unread");
    const unreadWithApproval = conversation("approval", 2);
    const alreadyRead = conversation("read");
    const conversations = [unread, unreadWithApproval, alreadyRead];
    seedReadState(conversations);
    markConversationUnread(unread.id, unread.updated_at);
    markConversationUnread(unreadWithApproval.id, unreadWithApproval.updated_at);
    toast("Copied", { id: "notice" });
    toast.error("Failed", { id: "error", duration: Number.POSITIVE_INFINITY });

    clearAllNotifications(conversations, ["comment-1", "comment-2"]);

    expect(isConversationUnseen(unread.id, unread.updated_at, unread.status)).toBe(false);
    expect(
      isConversationUnseen(
        unreadWithApproval.id,
        unreadWithApproval.updated_at,
        unreadWithApproval.status,
      ),
    ).toBe(false);
    expect(isConversationUnseen(alreadyRead.id, alreadyRead.updated_at, alreadyRead.status)).toBe(
      false,
    );
    expect(getSeenCommentIds()).toEqual(new Set(["comment-1", "comment-2"]));
    expect(unreadWithApproval.pending_elicitations_count).toBe(2);
    expect(activeToastIds()).toEqual([]);
  });

  it("keeps non-dismissible toasts that hold a pending decision", () => {
    toast("Copied", { id: "notice" });
    toast.custom(() => createElement("div"), {
      id: "clipboard-consent",
      dismissible: false,
      duration: Number.POSITIVE_INFINITY,
    });

    clearAllNotifications([], []);

    expect(activeToastIds()).toEqual(["clipboard-consent"]);
  });
});
