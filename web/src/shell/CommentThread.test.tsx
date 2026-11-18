import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { CommentReply } from "@/hooks/useCommentReplies";
import { CommentThread } from "./CommentThread";

const mocks = vi.hoisted(() => ({
  add: vi.fn(),
  remove: vi.fn(),
  send: vi.fn(),
  pending: false,
  replies: [] as CommentReply[],
  sender: true,
  query: vi.fn(),
}));
vi.mock("@/hooks/useCommentReplies", () => ({
  useCommentReplies: (...args: unknown[]) => {
    mocks.query(...args);
    return { data: mocks.replies };
  },
  useAddCommentReply: () => ({ mutateAsync: mocks.add, isPending: mocks.pending }),
  useDeleteCommentReply: () => ({ mutate: mocks.remove }),
}));
vi.mock("@/hooks/CommentSenderContext", () => ({
  useOptionalCommentSender: () => (mocks.sender ? { mutate: mocks.send } : null),
}));
vi.mock("@/lib/identity", () => ({ getCurrentAuthorId: () => "viewer@example.com" }));
const comment = {
  id: "comment",
  conversation_id: "session",
  reply_count: 4,
  last_reply: { author_kind: "agent", created_by: null, created_at: 0 },
} as Comment;
const now = new Date("2024-06-15T12:00:00Z");
function panel(props = {}) {
  return <CommentThread comment={comment} selected canEdit canAddress now={now} {...props} />;
}
function reply(id: string, author_kind: "user" | "agent", created_by: string | null): CommentReply {
  return {
    id,
    comment_id: "comment",
    conversation_id: "session",
    body: `Body ${id}`,
    author_kind,
    created_by,
    created_at: now.getTime() * 1000,
  };
}
beforeEach(() => {
  mocks.query.mockClear();
  mocks.add.mockReset().mockResolvedValue({});
  mocks.send.mockReset();
  mocks.remove.mockReset();
  mocks.pending = false;
  mocks.sender = true;
  mocks.replies = [
    reply("1", "user", null),
    reply("2", "user", "other@example.com"),
    reply("3", "user", "viewer@example.com"),
    reply("4", "agent", null),
  ];
});
afterEach(cleanup);
it("shows the latest three, expands earlier replies, and collapses to a summary", () => {
  const { rerender } = render(panel());
  expect(screen.queryByText("Body 1")).toBeNull();
  expect(screen.getByText("Body 4")).toBeTruthy();
  expect(screen.getAllByText(/Today/)).toHaveLength(3);
  fireEvent.click(screen.getByText("Show 1 earlier reply"));
  expect(screen.getByText("Body 1")).toBeTruthy();
  rerender(panel({ selected: false }));
  expect(screen.getByText("4 replies · last Agent")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
});
it("allows deleting only own user replies, including null authors", () => {
  render(panel());
  expect(screen.getAllByRole("button", { name: "Delete reply" })).toHaveLength(1);
  fireEvent.click(screen.getByText("Show 1 earlier reply"));
  const buttons = screen.getAllByRole("button", { name: "Delete reply" });
  expect(buttons).toHaveLength(2);
  fireEvent.click(buttons[0]!);
  expect(mocks.remove).toHaveBeenCalledWith("1", expect.anything());
});
it("keeps drafts on Cancel and Escape and saves Reply without sending", async () => {
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "  Follow up  " } });
  fireEvent.click(screen.getByText("Cancel"));
  expect(input).toHaveValue("  Follow up  ");
  fireEvent.focus(input);
  fireEvent.keyDown(input, { key: "Escape" });
  expect(input).toHaveValue("  Follow up  ");
  fireEvent.focus(input);
  fireEvent.click(screen.getByRole("button", { name: "Reply" }));
  await waitFor(() => expect(mocks.add).toHaveBeenCalledWith("Follow up"));
  expect(mocks.send).not.toHaveBeenCalled();
});
it("saves before Ask agent sends only this comment", async () => {
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Explain this" } });
  fireEvent.click(screen.getByText("Ask agent"));
  await waitFor(() =>
    expect(mocks.send).toHaveBeenCalledWith({ comment_ids: ["comment"] }, expect.anything()),
  );
  expect(mocks.add.mock.invocationCallOrder[0]).toBeLessThan(
    mocks.send.mock.invocationCallOrder[0]!,
  );
});
it("preserves failed drafts and never sends after failed POST", async () => {
  mocks.add.mockRejectedValue(new Error("Reply cap reached"));
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Keep me" } });
  fireEvent.click(screen.getByText("Ask agent"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reply cap reached");
  expect(input).toHaveValue("Keep me");
  expect(mocks.send).not.toHaveBeenCalled();
});
it("supports the submit shortcut and respects IME composition", async () => {
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Reply" } });
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
  expect(mocks.add).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: "Enter", metaKey: true });
  await waitFor(() => expect(mocks.add).toHaveBeenCalledOnce());
});
it("hides composer and delete controls for read-only access", () => {
  render(panel({ canEdit: false }));
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Delete reply" })).toBeNull();
});
it("hides Ask agent without an agent and disables empty or pending replies", () => {
  mocks.sender = false;
  const { rerender } = render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  expect(screen.queryByText("Ask agent")).toBeNull();
  expect(screen.getByRole("button", { name: "Reply" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "Draft" } });
  mocks.pending = true;
  rerender(panel());
  expect(screen.getByRole("button", { name: "Reply" })).toBeDisabled();
});

it("does not fetch unselected threads and uses only parent summary metadata", () => {
  mocks.replies = [];
  render(panel({ selected: false }));
  expect(mocks.query).toHaveBeenCalledWith("session", "comment", false);
  expect(screen.getByText("4 replies · last Agent")).toBeTruthy();
});
it("hides absent reply metadata on older servers", () => {
  render(
    panel({
      selected: false,
      comment: { ...comment, reply_count: undefined, last_reply: undefined },
    }),
  );
  expect(screen.queryByText(/replies/)).toBeNull();
});
