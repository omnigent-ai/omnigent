import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CommentsPanel } from "./CommentsPanel";

vi.mock("./CommentThread", () => ({ CommentThread: () => <div>Thread controls</div> }));
afterEach(cleanup);
function panel(enabled: boolean, anchor_metadata?: Comment["anchor_metadata"]) {
  const comment = {
    id: "c",
    conversation_id: "s",
    body: "Original comment",
    status: "draft",
    start_index: 0,
    end_index: 1,
    created_at: 0,
    created_by: null,
    reply_count: 4,
    anchor_metadata,
  } as Comment;
  render(
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, comment_replies: enabled }}>
      <CommentsPanel
        comments={[comment]}
        addressedComments={[]}
        activeSelection={null}
        onAddComment={vi.fn()}
        onAddressAll={vi.fn()}
        onEditComment={vi.fn()}
        onDeleteComment={vi.fn()}
        onClickComment={vi.fn()}
        canAddress={false}
        addressPending={false}
      />
    </CapabilitiesProvider>,
  );
}
it("keeps the original card without thread controls when capability is absent", () => {
  panel(false);
  expect(screen.getByText("Original comment")).toBeTruthy();
  expect(screen.queryByText("Thread controls")).toBeNull();
});
it("mounts threads when supported", () => {
  panel(true);
  expect(screen.getByText("Thread controls")).toBeTruthy();
});
it("excludes local PR review comments", () => {
  panel(true, { kind: "local_review" } as unknown as Comment["anchor_metadata"]);
  expect(screen.queryByText("Thread controls")).toBeNull();
});
