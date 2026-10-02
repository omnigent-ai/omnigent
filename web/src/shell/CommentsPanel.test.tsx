// Tests for CommentsPanel copy-comment-link affordance.
//
// Coverage:
//   1. Link button appears for each open comment when onCopyCommentLink is supplied.
//   2. Clicking the link button fires onCopyCommentLink with the correct comment ID.
//   3. Link button appears for addressed comments too (after switching the tab).
//   4. No link button is rendered when onCopyCommentLink is omitted.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { getCurrentAuthorId } from "@/lib/identity";
import type { ActiveSelection } from "./codeViewerHelpers";
import { CommentsPanel } from "./CommentsPanel";
import { resetCommentsSizeStoreForTesting } from "@/hooks/useResizableCommentsPanel";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";

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

function makeComment(
  id: string,
  status: Comment["status"] = "draft",
  range: { start_index: number; end_index: number } = { start_index: 0, end_index: 5 },
): Comment {
  return {
    id,
    conversation_id: "conv_1",
    path: "file.py",
    start_index: range.start_index,
    end_index: range.end_index,
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
      name: "Copy link to comment",
    });
    expect(linkButtons).toHaveLength(2);
  });

  it("calls onCopyCommentLink with the correct comment ID when the link button is clicked", () => {
    const onCopyCommentLink = vi.fn();
    renderPanel([makeComment("c1")], [], onCopyCommentLink);

    fireEvent.click(screen.getByRole("button", { name: "Copy link to comment" }));

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
    expect(screen.queryByRole("button", { name: "Copy link to comment" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /addressed/i }));

    // After switching tabs, the addressed comment is visible and has a link button.
    // Failure: onCopyLink was not passed to CommentCard for addressed comments.
    const linkButton = screen.getByRole("button", { name: "Copy link to comment" });
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

    expect(screen.queryByRole("button", { name: "Copy link to comment" })).toBeNull();
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
    expect(screen.getByText("You have read-only access to this session.")).toBeInTheDocument();
  });

  it("does not show the read-only banner for editors (canEdit true)", () => {
    renderGated({ canEdit: true });
    expect(screen.queryByText("You have read-only access to this session.")).toBeNull();
  });

  it("hides the add-comment form for a fresh selection when read-only", () => {
    // With a fresh selection an editor would get the compose form; a
    // read-only viewer must not, so they cannot create comments at all.
    renderGated({ canEdit: false, activeSelection: FRESH_SELECTION });
    expect(screen.queryByPlaceholderText("Add a comment…")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add Comment" })).toBeNull();
  });

  it("shows the add-comment form for the same selection when editing is allowed", () => {
    renderGated({ canEdit: true, activeSelection: FRESH_SELECTION });
    expect(screen.getByPlaceholderText("Add a comment…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add Comment" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Copy link to comment" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Copy link to comment" })).toBeInTheDocument();
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
      const toggle = screen.getByRole("button", { name: "Show more" });

      // Expand: clamp class drops and the label flips to "Show less".
      fireEvent.click(toggle);
      expect(body.className).not.toContain("line-clamp-4");
      expect(screen.getByRole("button", { name: "Show less" })).toBeInTheDocument();

      // Collapse again: back to clamped + "Show more".
      fireEvent.click(screen.getByRole("button", { name: "Show less" }));
      expect(body.className).toContain("line-clamp-4");
      expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
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

      expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Show less" })).toBeNull();
    } finally {
      restore();
    }
  });
});

// ── Resize affordance (width handle beside the viewer, height handle under it) ──
//
// Parent-row size, not viewport size, controls the handle's axis and limits.

const rows: HTMLElement[] = [];
afterEach(() => rows.splice(0).forEach((row) => row.remove()));

/** Render the panel inside a parent row whose pixel size the arguments report. */
function renderPanelInRow(rowWidth: number | (() => number), rowHeight = 600) {
  const width = typeof rowWidth === "function" ? rowWidth : () => rowWidth;
  const row = document.createElement("div");
  rows.push(row);
  row.getBoundingClientRect = () =>
    ({
      width: width(),
      height: rowHeight,
      top: 0,
      left: 0,
      right: width(),
      bottom: rowHeight,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }) as DOMRect;
  document.body.appendChild(row);
  return render(
    <CommentsPanel
      comments={[makeComment("c1")]}
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
    { container: row },
  );
}

describe("CommentsPanel resize affordance", () => {
  afterEach(() => {
    window.localStorage.clear();
    resetCommentsSizeStoreForTesting();
  });

  it("renders a resize handle and exposes the side-by-side width beside a wide viewer row", () => {
    renderPanelInRow(800);

    // The separator is the drag handle; its parent is the panel root, which
    // carries the side-by-side width (default 240px) as a CSS variable that
    // the container query applies only beside the viewer.
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    const panel = handle.parentElement as HTMLElement;
    expect(panel.style.getPropertyValue("--comments-panel-width")).toBe("240px");
    expect(panel.style.width).toBe("");
  });

  it("shrinks the default width so a tight viewer row keeps 240px for the viewer", () => {
    // 472px row - 240px viewer minimum = 232px for the panel.
    renderPanelInRow(472);
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    const panel = handle.parentElement as HTMLElement;
    expect(panel.style.getPropertyValue("--comments-panel-width")).toBe("232px");
  });

  it("turns the handle into a height handle when the panel stacks under a narrow row", () => {
    renderPanelInRow(220);
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    expect(handle.getAttribute("aria-orientation")).toBe("horizontal");
    const panel = handle.parentElement as HTMLElement;
    const height = () => panel.style.getPropertyValue("--comments-panel-height");
    expect(height()).toBe("256px");

    // Up/Down arrows change the stacked height and persist the choice.
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(height()).toBe("276px");
    expect(readPanelSizePreference("commentsPanelHeightPx")).toBe(276);
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    expect(height()).toBe("236px");
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
  });

  it("caps the stacked height so the viewer keeps 160px above the panel", () => {
    // 360px row - 160px viewer minimum = 200px for the panel.
    renderPanelInRow(220, 360);
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    const panel = handle.parentElement as HTMLElement;
    expect(panel.style.getPropertyValue("--comments-panel-height")).toBe("200px");
  });

  it("drags the stacked panel's top edge to change its height", () => {
    renderPanelInRow(220);
    const handle = screen.getByRole("separator", { name: "Resize comments panel" });
    const panel = handle.parentElement as HTMLElement;
    panel.getBoundingClientRect = () =>
      ({
        x: 0,
        y: 344,
        top: 344,
        left: 0,
        right: 220,
        bottom: 600,
        width: 220,
        height: 256,
        toJSON: () => ({}),
      }) as DOMRect;

    fireEvent.mouseDown(handle);
    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientY: 300 }));
    });
    // The handle is the panel's top edge: height = panel bottom - pointer y.
    expect(panel.style.getPropertyValue("--comments-panel-height")).toBe("300px");
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
    expect(readPanelSizePreference("commentsPanelHeightPx")).toBe(300);
  });

  it("follows the row across the breakpoint and restores the preferred width", () => {
    const observers: ResizeObserverCallback[] = [];
    class StubResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observers.push(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    let rowWidth = 800;
    try {
      renderPanelInRow(() => rowWidth);
      const handle = () => screen.getByRole("separator", { name: "Resize comments panel" });
      const orientation = () => handle().getAttribute("aria-orientation");
      const panel = handle().parentElement as HTMLElement;
      const width = () => panel.style.getPropertyValue("--comments-panel-width");
      expect(orientation()).toBe("vertical");
      expect(width()).toBe("240px");

      // The user widens the panel by one keyboard step; that choice persists.
      fireEvent.keyDown(handle(), { key: "ArrowLeft" });
      expect(width()).toBe("260px");

      // The rail shrinks below the breakpoint: the panel stacks, the handle
      // becomes a height handle and the width clamps to the floor.
      rowWidth = 300;
      act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
      expect(orientation()).toBe("horizontal");
      expect(width()).toBe("200px");

      // The 28rem (448px) boundary shared with the container query.
      rowWidth = 447;
      act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
      expect(orientation()).toBe("horizontal");
      expect(width()).toBe("207px");
      rowWidth = 448;
      act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
      expect(orientation()).toBe("vertical");
      expect(width()).toBe("208px");

      // Space returns: the saved choice, not the default, is restored.
      rowWidth = 800;
      act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
      expect(orientation()).toBe("vertical");
      expect(width()).toBe("260px");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

// ── Active-comment reveal (selecting a highlight in the file) ──────────────
//
// Selecting a highlighted range in the file sets activeSelection to that
// comment's range. The panel must reveal the matching open card or the new-
// comment form and scroll it into view. scrollIntoView isn't implemented in
// jsdom, so it's stubbed to record the call.

function panelWithSelection(
  comments: Comment[],
  addressedComments: Comment[],
  activeSelection: ActiveSelection | null,
  onClickComment = vi.fn(),
) {
  return (
    <CommentsPanel
      comments={comments}
      addressedComments={addressedComments}
      activeSelection={activeSelection}
      onAddComment={vi.fn()}
      onAddressAll={vi.fn()}
      onEditComment={vi.fn()}
      onDeleteComment={vi.fn()}
      onClickComment={onClickComment}
      canAddress={false}
      addressPending={false}
    />
  );
}

function renderWithSelection(
  comments: Comment[],
  addressedComments: Comment[],
  activeSelection: ActiveSelection | null,
) {
  return render(panelWithSelection(comments, addressedComments, activeSelection));
}

/** Run pending requestAnimationFrame callbacks (the scroll effect defers via rAF). */
function flushRaf(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

describe("CommentsPanel active-comment reveal", () => {
  const originalScrollIntoView = Element.prototype.scrollIntoView;

  afterEach(() => {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  });

  it("scrolls the active comment's card into view", async () => {
    const scrollSpy = vi.fn();
    Element.prototype.scrollIntoView = scrollSpy;

    const target = makeComment("c2", "draft", { start_index: 40, end_index: 60 });
    renderWithSelection([makeComment("c1"), target], [], {
      start_index: 40,
      end_index: 60,
      anchor_content: "hello",
      comment_id: "c2",
    });
    await flushRaf();

    // Only the matching card scrolls, and it scrolls without yanking layout
    // when already visible (block: nearest).
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(scrollSpy).toHaveBeenCalledWith({ block: "nearest", behavior: "smooth" });
  });

  it("keeps a new selection open when an addressed comment shares its range", async () => {
    Element.prototype.scrollIntoView = vi.fn();

    const addressed = makeComment("c2", "addressed", { start_index: 40, end_index: 60 });
    renderWithSelection([], [addressed], {
      start_index: 40,
      end_index: 60,
      anchor_content: "hello",
    });
    await flushRaf();

    expect(screen.getByPlaceholderText("Add a comment…")).toBeInTheDocument();
    expect(screen.queryByText("Comment c2")).not.toBeInTheDocument();
  });

  it("follows a selected comment into Addressed once without locking the tabs", async () => {
    Element.prototype.scrollIntoView = vi.fn();

    // Matching ids model one comment changing status; only one tab renders at a time.
    const open = makeComment("c2", "draft", { start_index: 40, end_index: 60 });
    const addressed = makeComment("c2", "addressed", { start_index: 40, end_index: 60 });
    const activeSelection: ActiveSelection = {
      start_index: 40,
      end_index: 60,
      anchor_content: "hello",
      comment_id: "c2",
    };
    const { rerender } = renderWithSelection([open], [], activeSelection);

    rerender(panelWithSelection([], [addressed], activeSelection));
    await flushRaf();
    expect(screen.getByText("Comment c2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Address All" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Open/ }));
    expect(screen.getByText("No open comments.")).toBeInTheDocument();

    rerender(panelWithSelection([], [{ ...addressed }], activeSelection));
    await flushRaf();
    expect(screen.getByText("No open comments.")).toBeInTheDocument();
  });

  it("activates an addressed comment when its card is clicked", () => {
    const addressed = makeComment("c2", "addressed");
    const onClickComment = vi.fn();
    render(panelWithSelection([], [addressed], null, onClickComment));

    fireEvent.click(screen.getByRole("button", { name: /^Addressed/ }));
    fireEvent.click(screen.getByText("Comment c2"));

    expect(onClickComment).toHaveBeenCalledWith(addressed);
  });
});
