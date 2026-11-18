import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { isMacPlatform } from "@/lib/hotkeys";
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
  senderPending: false,
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
  useOptionalCommentSender: () =>
    mocks.sender ? { mutateAsync: mocks.send, isPending: mocks.senderPending } : null,
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
  mocks.send.mockReset().mockResolvedValue({});
  mocks.senderPending = false;
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
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ comment_ids: ["comment"] }));
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
  fireEvent.keyDown(input, {
    key: "Enter",
    ...(isMacPlatform() ? { metaKey: true } : { ctrlKey: true }),
  });
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
  expect(mocks.query).not.toHaveBeenCalled();
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

it.each([0, 4])(
  "opens an unselected thread with %s replies using the keyboard",
  async (reply_count) => {
    const user = userEvent.setup();
    const onSelect = vi.fn(() => view.rerender(panel()));
    const view = render(panel({ selected: false, comment: { ...comment, reply_count }, onSelect }));
    expect(mocks.query).not.toHaveBeenCalled();
    const button = screen.getByRole("button", {
      name: reply_count ? "4 replies · last Agent" : "Reply…",
    });
    await user.tab();
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox")).toBeTruthy();
  },
);

it("keeps a pending shortcut submission focused and read-only, then restores a failed draft", async () => {
  let rejectSave!: (reason: Error) => void;
  mocks.add.mockReturnValue(
    new Promise((_resolve, reject) => {
      rejectSave = reject;
    }),
  );
  const { rerender } = render(panel());
  const input = screen.getByRole("textbox");
  input.focus();
  fireEvent.change(input, { target: { value: "Keep draft" } });
  fireEvent.keyDown(input, {
    key: "Enter",
    ...(isMacPlatform() ? { metaKey: true } : { ctrlKey: true }),
  });
  mocks.pending = true;
  rerender(panel());
  expect(input).not.toBeDisabled();
  expect(input).toHaveAttribute("readonly");
  expect(input).toHaveAttribute("aria-busy", "true");
  expect(input).toHaveFocus();
  rejectSave(new Error("Thread is full"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Thread is full");
  expect(input).toHaveFocus();
  expect(input).toHaveValue("Keep draft");
  fireEvent.change(input, { target: { value: "Edited draft" } });
  expect(screen.queryByRole("alert")).toBeNull();
});

it("ignores shared sender pending state on a different card", () => {
  mocks.senderPending = true;
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "My draft" } });
  expect(input).not.toHaveAttribute("readonly");
  expect(screen.getByRole("button", { name: "Reply" })).not.toBeDisabled();
  expect(screen.getByRole("button", { name: "Ask agent" })).not.toBeDisabled();
});

it("reports Ask agent send failures without reposting the saved reply", async () => {
  mocks.send.mockRejectedValue(new Error("Agent unavailable"));
  render(panel());
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Please explain" } });
  fireEvent.click(screen.getByText("Ask agent"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Reply saved. Could not ask agent: Agent unavailable",
  );
  expect(mocks.add).toHaveBeenCalledOnce();
  expect(input).toHaveValue("");
  expect(input).toHaveFocus();
});

it.each([true, false])("restores composer focus after delete (success=%s)", async (success) => {
  mocks.remove.mockImplementation((_id, options) => {
    if (success) options.onSuccess();
    else options.onError(new Error("Cannot delete reply"));
  });
  render(panel());
  fireEvent.click(screen.getByRole("button", { name: "Delete reply" }));
  expect(screen.getByRole("textbox")).toHaveFocus();
  if (!success) expect(screen.getByRole("alert")).toHaveTextContent("Cannot delete reply");
});

it("focuses the first revealed reply and resets disclosure/errors while retaining drafts", async () => {
  mocks.add.mockRejectedValue(new Error("Try again"));
  const { rerender } = render(panel());
  fireEvent.click(screen.getByText("Show 1 earlier reply"));
  expect(screen.getByText("Body 1").closest('[tabindex="-1"]')).toHaveFocus();
  const input = screen.getByRole("textbox");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Retained" } });
  fireEvent.click(screen.getByRole("button", { name: "Reply" }));
  await screen.findByRole("alert");
  rerender(panel({ selected: false }));
  rerender(panel());
  expect(screen.getByRole("textbox")).toHaveValue("Retained");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByText("Body 1")).toBeNull();
  rerender(panel({ comment: { ...comment, id: "different-comment" } }));
  expect(screen.getByRole("textbox")).toHaveValue("");
});
