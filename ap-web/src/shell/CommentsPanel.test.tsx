// Tests for CommentsPanel copy-comment-link affordance.
//
// Coverage:
//   1. Link button appears for each open comment when onCopyCommentLink is supplied.
//   2. Clicking the link button fires onCopyCommentLink with the correct comment ID.
//   3. Link button appears for addressed comments too (after switching the tab).
//   4. No link button is rendered when onCopyCommentLink is omitted.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import i18n from "@/i18n";
import { getCurrentAuthorId } from "@/lib/identity";
import type { ActiveSelection } from "./codeViewerHelpers";
import { CommentsPanel } from "./CommentsPanel";

const t = i18n.getFixedT(null, "common");

// CommentsPanel reads the current user's identity (getCurrentAuthorId) to
// decide whose comments expose Edit/Delete. Mock it so author-ownership tests
// can pin "who am I". The default null mirrors single-user/local mode and an
// unresolved identity, which is what the pre-existing (null-author) fixtures
// below rely on — under that default every comment is treated as own.
vi.mock("@/lib/identity", () => ({
  getCurrentAuthorId: vi.fn<() => string | null>(() => null),
}));
const mockGetCurrentAuthorId = vi.mocked(getCurrentAuthorId);

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeComment(id: string, status: Comment["status"] = "draft"): Comment {
  return {
    id,
    conversation_id: "conv_1",
    path: "file.py",
    start_index: 0,
    end_index: 5,
    body: `Comment ${id}`,
    status,
    created_at: 0,
    updated_at: 0,
    anchor_content: "hello",
    created_by: null,
  };
}

/** Minimal prop set for CommentsPanel — only the fields under test vary. */
function renderPanel(
  comments: Comment[],
  addressedComments: Comment[],
  onCopyCommentLink?: (id: string) => void,
) {
  return render(
    <CommentsPanel
      comments={comments}
      addressedComments={addressedComments}
      activeSelection={null}
      onAddComment={vi.fn()}
      onAddressAll={vi.fn()}
      onEditComment={vi.fn()}
      onDeleteComment={vi.fn()}
      onClickComment={vi.fn()}
      canAddress={false}
      addressPending={false}
      onCopyCommentLink={onCopyCommentLink}
    />,
  );
}

afterEach(cleanup);

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("CommentsPanel copy-comment-link", () => {
  it("shows a link button for each open comment when onCopyCommentLink is provided", () => {
    renderPanel([makeComment("c1"), makeComment("c2")], [], vi.fn());

    // Two open comments → two link buttons. Failure means onCopyCommentLink
    // was not wired through CommentsPanel → CommentCard, or the button was not rendered.
    const linkButtons = screen.getAllByRole("button", {
      name: t("copyLinkToComment"),
    });
    expect(linkButtons).toHaveLength(2);
  });

  it("calls onCopyCommentLink with the correct comment ID when the link button is clicked", () => {
    const onCopyCommentLink = vi.fn();
    renderPanel([makeComment("c1")], [], onCopyCommentLink);

    fireEvent.click(screen.getByRole("button", { name: t("copyLinkToComment") }));

    // Must receive "c1", not undefined or a different ID.
    // Failure: callback not called, or wrong argument passed (e.g. the full Comment object).
    expect(onCopyCommentLink).toHaveBeenCalledTimes(1);
    expect(onCopyCommentLink).toHaveBeenCalledWith("c1");
  });

  it("shows a link button for addressed comments after switching to the Addressed tab", () => {
    const addressedComment = makeComment("c2", "addressed");
    const onCopyCommentLink = vi.fn();
    renderPanel([], [addressedComment], onCopyCommentLink);

    // The default view is "Open". Addressed comments are hidden until the user
    // clicks the Addressed tab.
    expect(screen.queryByRole("button", { name: t("copyLinkToComment") })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /addressed/i }));

    // After switching tabs, the addressed comment is visible and has a link button.
    // Failure: onCopyLink was not passed to CommentCard for addressed comments.
    const linkButton = screen.getByRole("button", { name: t("copyLinkToComment") });
    expect(linkButton).toBeInTheDocument();

    // The addressed list is a SEPARATE onCopyLink call site from the open list,
    // so verify it fires with the addressed comment's own ID (not the open path's).
    fireEvent.click(linkButton);
    expect(onCopyCommentLink).toHaveBeenCalledTimes(1);
    expect(onCopyCommentLink).toHaveBeenCalledWith("c2");
  });

  it("does not show any link buttons when onCopyCommentLink is not provided", () => {
    // Without a handler, no link button should be rendered — the prop is optional.
    // Failure: button was rendered unconditionally, ignoring the absence of the prop.
    renderPanel([makeComment("c1")], []);

    expect(screen.queryByRole("button", { name: t("copyLinkToComment") })).toBeNull();
  });
});

// ── Read-only collaborator gating (canEdit) ────────────────────────────────
//
// A shared session opened by a read-only collaborator (permission level 1)
// renders the panel with canEdit=false. The panel must then suppress every
// mutation affordance — add-comment form, per-comment Edit, per-comment
// Delete — while still letting the viewer read comments and copy links.
// Edit-or-higher collaborators (canEdit=true, the default) keep all of them.

// A fresh selection whose range matches no existing comment, so the panel's
// "add a comment here" form becomes eligible to render (gated on canEdit).
const FRESH_SELECTION: ActiveSelection = {
  start_index: 10,
  end_index: 20,
  anchor_content: "selected text",
};

function renderGated(opts: {
  canEdit: boolean;
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  handlers?: Partial<{
    onAddComment: (body: string) => void;
    onEditComment: (id: string, body: string) => void;
    onDeleteComment: (id: string) => void;
  }>;
}) {
  return render(
    <CommentsPanel
      comments={opts.comments ?? []}
      addressedComments={[]}
      activeSelection={opts.activeSelection ?? null}
      onAddComment={opts.handlers?.onAddComment ?? vi.fn()}
      onAddressAll={vi.fn()}
      onEditComment={opts.handlers?.onEditComment ?? vi.fn()}
      onDeleteComment={opts.handlers?.onDeleteComment ?? vi.fn()}
      onClickComment={vi.fn()}
      canAddress={false}
      addressPending={false}
      canEdit={opts.canEdit}
      onCopyCommentLink={vi.fn()}
    />,
  );
}

describe("CommentsPanel read-only collaborator gating", () => {
  it("shows the read-only banner when canEdit is false", () => {
    renderGated({ canEdit: false });
    expect(screen.getByText(t("readOnlyAccess"))).toBeInTheDocument();
  });

  it("does not show the read-only banner for editors (canEdit true)", () => {
    renderGated({ canEdit: true });
    expect(screen.queryByText(t("readOnlyAccess"))).toBeNull();
  });

  it("hides the add-comment form for a fresh selection when read-only", () => {
    // With a fresh selection an editor would get the compose form; a
    // read-only viewer must not, so they cannot create comments at all.
    renderGated({ canEdit: false, activeSelection: FRESH_SELECTION });
    expect(screen.queryByPlaceholderText(t("addACommentPlaceholder"))).toBeNull();
    expect(screen.queryByRole("button", { name: t("addComment") })).toBeNull();
  });

  it("shows the add-comment form for the same selection when editing is allowed", () => {
    renderGated({ canEdit: true, activeSelection: FRESH_SELECTION });
    expect(screen.getByPlaceholderText(t("addACommentPlaceholder"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("addComment") })).toBeInTheDocument();
  });

  it("hides per-comment Edit and Delete actions when read-only", () => {
    renderGated({ canEdit: false, comments: [makeComment("c1")] });
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("shows per-comment Edit and Delete actions for editors", () => {
    renderGated({ canEdit: true, comments: [makeComment("c1")] });
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("still lets a read-only viewer read comments and copy links", () => {
    // Read-only gates *mutation*, not visibility — the comment body and
    // the copy-link affordance remain available.
    renderGated({ canEdit: false, comments: [makeComment("c1")] });
    expect(screen.getByText("Comment c1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("copyLinkToComment") })).toBeInTheDocument();
  });
});

// ── Author-only edit/delete gating (created_by vs current user) ─────────────
//
// Even with edit access (canEdit=true), a collaborator may only edit/delete
// their OWN comments. The panel compares each comment's created_by against
// getCurrentAuthorId and exposes Edit/Delete only on a match (the backend
// enforces this independently; the UI just hides the affordances). Comments
// with no recorded author (created_by null — legacy / single-user) stay
// editable by any editor.

/** A draft comment authored by `author`, otherwise identical to makeComment. */
function makeAuthoredComment(id: string, author: string | null): Comment {
  return { ...makeComment(id), created_by: author };
}

describe("CommentsPanel author-only edit/delete gating", () => {
  afterEach(() => {
    // Restore the default identity so later describe blocks (and any test
    // order) see the null-author behavior their fixtures assume.
    mockGetCurrentAuthorId.mockReturnValue(null);
  });

  it("shows Edit/Delete on the current user's own comment", () => {
    mockGetCurrentAuthorId.mockReturnValue("alice@example.com");
    renderGated({
      canEdit: true,
      comments: [makeAuthoredComment("c1", "alice@example.com")],
    });
    // Alice authored c1 → her own affordances appear. Failure means
    // canModify rejected a self-authored comment (over-restriction).
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("hides Edit/Delete on another user's comment even with edit access", () => {
    mockGetCurrentAuthorId.mockReturnValue("bob@example.com");
    renderGated({
      canEdit: true,
      comments: [makeAuthoredComment("c1", "alice@example.com")],
    });
    // Bob is an editor but did NOT author c1 → no mutation affordances.
    // Failure here is the actual bug being fixed: an editor able to edit or
    // delete another user's comment from the UI.
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    // Reading and copy-link stay available — gating is on mutation only.
    expect(screen.getByText("Comment c1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("copyLinkToComment") })).toBeInTheDocument();
  });

  it("shows Edit/Delete on an authorless (legacy/single-user) comment", () => {
    mockGetCurrentAuthorId.mockReturnValue("bob@example.com");
    renderGated({
      canEdit: true,
      comments: [makeAuthoredComment("c1", null)],
    });
    // created_by null → no author to protect, so any editor may modify,
    // matching the backend's `created_by is None` fallback. Failure would
    // mean legacy comments became uneditable after this change.
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("renders own-vs-others affordances correctly in a mixed-author list", () => {
    mockGetCurrentAuthorId.mockReturnValue("alice@example.com");
    renderGated({
      canEdit: true,
      comments: [
        makeAuthoredComment("c1", "alice@example.com"),
        makeAuthoredComment("c2", "bob@example.com"),
      ],
    });
    // Exactly one Edit and one Delete button — Alice's c1 only, not Bob's c2.
    // A count of 2 would mean Bob's comment leaked mutation affordances; 0
    // would mean Alice's own comment was wrongly suppressed.
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);
  });
});

// ── Show more / less (long comment bodies) ──────────────────────────────────
//
// A long body is clamped to a few lines with a Google-Docs-style "Show more"
// toggle. jsdom does no layout (scrollHeight/clientHeight are 0), so we mock
// the element dimensions to simulate overflow vs. fit.

/**
 * Override layout metrics so the clamp-overflow check has real numbers
 * (jsdom does no layout). Returns a restore fn.
 *
 * scrollHeight/clientHeight live on `Element.prototype`, NOT on
 * `HTMLElement.prototype`, so there is no own descriptor to capture here —
 * we must DELETE the own property we add on restore, otherwise the mocked
 * value shadows the inherited getter for every later test in the suite.
 */
function mockLayoutMetrics(scrollHeight: number, clientHeight: number): () => void {
  const origScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
  const origClient = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    value: scrollHeight,
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    value: clientHeight,
  });
  return () => {
    if (origScroll) Object.defineProperty(HTMLElement.prototype, "scrollHeight", origScroll);
    else delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight;
    if (origClient) Object.defineProperty(HTMLElement.prototype, "clientHeight", origClient);
    else delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight;
  };
}

const LONG_BODY =
  "This is a long comment body that overflows the collapsed clamp and needs a toggle.";

function makeCommentWithBody(id: string, body: string): Comment {
  return { ...makeComment(id), body };
}

describe("CommentsPanel show more / less", () => {
  it("shows a working Show more/less toggle when the body overflows the clamp", () => {
    // scrollHeight > clientHeight → the body is clamped, so the toggle appears.
    const restore = mockLayoutMetrics(200, 50);
    try {
      render(
        <CommentsPanel
          comments={[makeCommentWithBody("c1", LONG_BODY)]}
          addressedComments={[]}
          activeSelection={null}
          onAddComment={vi.fn()}
          onAddressAll={vi.fn()}
          onEditComment={vi.fn()}
          onDeleteComment={vi.fn()}
          onClickComment={vi.fn()}
          canAddress={false}
          addressPending={false}
        />,
      );

      // Collapsed: toggle reads "Show more" and the body carries the clamp class.
      const body = screen.getByText(LONG_BODY);
      expect(body.className).toContain("line-clamp-4");
      const toggle = screen.getByRole("button", { name: t("showMore") });

      // Expand: clamp class drops and the label flips to "Show less".
      fireEvent.click(toggle);
      expect(body.className).not.toContain("line-clamp-4");
      expect(screen.getByRole("button", { name: t("showLess") })).toBeInTheDocument();

      // Collapse again: back to clamped + "Show more".
      fireEvent.click(screen.getByRole("button", { name: t("showLess") }));
      expect(body.className).toContain("line-clamp-4");
      expect(screen.getByRole("button", { name: t("showMore") })).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("does not render a toggle when the body fits within the clamp", () => {
    // scrollHeight == clientHeight → no overflow, so no toggle is offered.
    const restore = mockLayoutMetrics(50, 50);
    try {
      render(
        <CommentsPanel
          comments={[makeCommentWithBody("c1", "short")]}
          addressedComments={[]}
          activeSelection={null}
          onAddComment={vi.fn()}
          onAddressAll={vi.fn()}
          onEditComment={vi.fn()}
          onDeleteComment={vi.fn()}
          onClickComment={vi.fn()}
          canAddress={false}
          addressPending={false}
        />,
      );

      expect(screen.queryByRole("button", { name: t("showMore") })).toBeNull();
      expect(screen.queryByRole("button", { name: t("showLess") })).toBeNull();
    } finally {
      restore();
    }
  });
});

// ── Resize affordance (desktop-only width handle) ───────────────────────────
//
// The panel is resizable on desktop via a left-edge drag handle, and stacks
// full-width (no inline width, no handle) on a narrow/mobile viewport. Desktop
// vs mobile is decided from window.innerWidth (jsdom defaults to 1024 ≥ md).

describe("CommentsPanel resize affordance", () => {
  it("renders a resize handle and applies an inline width on desktop", () => {
    renderPanel([makeComment("c1")], []);

    // The separator is the drag handle; its parent is the panel root, which
    // gets an explicit pixel width (default 240px) so it can be dragged wider.
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    expect((handle.parentElement as HTMLElement).style.width).toBe("240px");
  });

  it("omits the handle and inline width on a narrow (mobile) viewport", () => {
    const orig = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 500 });
    try {
      renderPanel([makeComment("c1")], []);
      // No drag handle, and the panel falls back to the w-full class (no inline width).
      expect(screen.queryByRole("separator", { name: "Resize comments panel" })).toBeNull();
      const panel = screen.getByText(t("commentsTitle")).closest("div")?.parentElement as HTMLElement;
      expect(panel.style.width).toBe("");
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: orig });
    }
  });
});
