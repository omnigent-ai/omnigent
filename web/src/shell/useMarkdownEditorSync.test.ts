// Unit tests for useMarkdownEditorSync — focusing on the external-update
// (hasExternalUpdate / discardAndApplyExternal / dismissExternalUpdate) logic
// added to surface server-side file changes that arrived while the editor
// was dirty.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderSync(initial: { content: string; path?: string; isSettled?: boolean }) {
  return renderHook(
    (props: { content: string; path: string; isSettled: boolean }) =>
      useMarkdownEditorSync({ ...props, onDirtyChange: undefined }),
    {
      initialProps: {
        content: initial.content,
        path: initial.path ?? "/file.md",
        isSettled: initial.isSettled ?? true,
      },
    },
  );
}

// ---------------------------------------------------------------------------
// setContentRef — in-place update path
// ---------------------------------------------------------------------------

describe("setContentRef", () => {
  it("calls setContentRef instead of incrementing editorKey on clean external update", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };

    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );
    const keyBefore = result.current.editorKey;

    rerender({ content: "v2", path: "/file.md", isSettled: true });

    // In-place update — no remount.
    expect(result.current.editorKey).toBe(keyBefore);
    expect(setContentFn).toHaveBeenCalledWith("v2");
  });

  it("falls back to editorKey increment when setContentRef.current is null", () => {
    const setContentRef = { current: null };

    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );
    const keyBefore = result.current.editorKey;

    rerender({ content: "v2", path: "/file.md", isSettled: true });

    // Fallback: editor not ready, remount.
    expect(result.current.editorKey).toBeGreaterThan(keyBefore);
  });

  it("calls setContentRef when dirty editor becomes clean with pending content", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };

    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(true);
    const keyBefore = result.current.editorKey;

    // Save completes → editor becomes clean → stashed content applied in-place.
    act(() => {
      result.current.setDirty(false);
    });

    expect(result.current.editorKey).toBe(keyBefore);
    expect(setContentFn).toHaveBeenCalledWith("v2");
  });

  it("calls setContentRef in discardAndApplyExternal instead of remounting", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };

    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );
    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    const keyBefore = result.current.editorKey;

    act(() => {
      result.current.discardAndApplyExternal();
    });

    expect(result.current.editorKey).toBe(keyBefore);
    expect(setContentFn).toHaveBeenCalledWith("v2");
    expect(result.current.hasExternalUpdate).toBe(false);
    expect(result.current.isDirty).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// markSaved — self-save echo dedupe
// ---------------------------------------------------------------------------

describe("markSaved echo dedupe", () => {
  it("ignores the server echo of content this editor just saved while dirty", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };
    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );

    // User is mid-edit (dirty); an auto-save persists "v2" and records it.
    act(() => {
      result.current.setDirty(true);
    });
    act(() => {
      result.current.markSaved("v2");
    });
    // The saved text echoes back through the file-content query.
    rerender({ content: "v2", path: "/file.md", isSettled: true });

    // Our own write must not be mistaken for an external edit (which would
    // pop the conflict banner) nor re-applied over the live cursor.
    expect(result.current.hasExternalUpdate).toBe(false);
    expect(setContentFn).not.toHaveBeenCalled();
  });

  it("ignores the saved echo on a clean editor without remounting", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };
    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );
    const keyBefore = result.current.editorKey;

    act(() => {
      result.current.markSaved("v2");
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });

    // No setContent (would reset the cursor) and no remount: the editor
    // already holds exactly this content.
    expect(setContentFn).not.toHaveBeenCalled();
    expect(result.current.editorKey).toBe(keyBefore);
  });

  it("still flags a genuine external edit that differs from the last save", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    act(() => {
      result.current.markSaved("v2");
    });
    // Server reports v3 — a real external change, not our echo.
    rerender({ content: "v3", path: "/file.md", isSettled: true });

    // Differs from the recorded save → must surface as an external update.
    expect(result.current.hasExternalUpdate).toBe(true);
  });

  it("resets the saved marker on path change so it can't suppress a new file's update", () => {
    const { result, rerender } = renderSync({ content: "v1", path: "/a.md" });

    act(() => {
      result.current.markSaved("shared");
    });
    // Switch to a different file.
    rerender({ content: "b1", path: "/b.md", isSettled: true });
    // Edit b.md, then receive an external update that coincidentally equals
    // a.md's last-saved text. The marker must have been cleared on the path
    // change, so this is treated as a real external edit (not a stale echo).
    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "shared", path: "/b.md", isSettled: true });

    expect(result.current.hasExternalUpdate).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// reconcileServerContent — pre-write conflict check
// ---------------------------------------------------------------------------

describe("reconcileServerContent", () => {
  it("returns false when the fetched content matches the last-known server state", () => {
    const { result } = renderSync({ content: "v1" });
    let conflict = true;
    act(() => {
      conflict = result.current.reconcileServerContent("v1");
    });
    // Server is unchanged → no conflict → the caller may write.
    expect(conflict).toBe(false);
    expect(result.current.hasExternalUpdate).toBe(false);
  });

  it("returns false when the fetched content matches our own last save", () => {
    const { result } = renderSync({ content: "v1" });
    act(() => {
      result.current.setDirty(true);
    });
    act(() => {
      result.current.markSaved("v2");
    });
    let conflict = true;
    // The server reflects exactly what we last wrote — not an external edit.
    act(() => {
      conflict = result.current.reconcileServerContent("v2");
    });
    expect(conflict).toBe(false);
    expect(result.current.hasExternalUpdate).toBe(false);
  });

  it("raises a conflict (returns true) when dirty and the server changed externally", () => {
    const { result } = renderSync({ content: "v1" });
    act(() => {
      result.current.setDirty(true);
    });
    let conflict = false;
    // Agent wrote "agent" while the user has unsaved edits → must block the
    // clobbering write and surface Keep mine / Load latest.
    act(() => {
      conflict = result.current.reconcileServerContent("agent");
    });
    expect(conflict).toBe(true);
    expect(result.current.hasExternalUpdate).toBe(true);
  });

  it("adopts new content without conflict when the editor is clean", () => {
    const setContentFn = vi.fn();
    const setContentRef = { current: setContentFn };
    const { result } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange: undefined, setContentRef }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );
    let conflict = true;
    act(() => {
      conflict = result.current.reconcileServerContent("v2");
    });
    // Clean editor → nothing to conflict with → adopt the content in place.
    expect(conflict).toBe(false);
    expect(setContentFn).toHaveBeenCalledWith("v2");
  });

  it("lets a 'Keep mine' overwrite through after acknowledging the conflict", () => {
    const { result } = renderSync({ content: "v1" });
    act(() => {
      result.current.setDirty(true);
    });
    act(() => {
      result.current.reconcileServerContent("agent");
    });
    expect(result.current.hasExternalUpdate).toBe(true);

    // User clicks Keep mine.
    act(() => {
      result.current.dismissExternalUpdate();
    });

    // The next pre-write check sees the same (now acknowledged) server
    // content → no conflict → the overwrite proceeds. Proves the external
    // content was recorded as the known baseline.
    let conflict = true;
    act(() => {
      conflict = result.current.reconcileServerContent("agent");
    });
    expect(conflict).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// hasExternalUpdate
// ---------------------------------------------------------------------------

describe("hasExternalUpdate", () => {
  it("is false on initial render", () => {
    const { result } = renderSync({ content: "v1" });
    expect(result.current.hasExternalUpdate).toBe(false);
  });

  it("is false when content changes on a clean editor", () => {
    const { result, rerender } = renderSync({ content: "v1" });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(false);
  });

  it("becomes true when content changes while the editor is dirty", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });

    expect(result.current.hasExternalUpdate).toBe(true);
  });

  it("clears automatically when the editor becomes clean (normal save path)", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(true);

    // Simulate save completing → dirty clears → stash applied
    act(() => {
      result.current.setDirty(false);
    });
    expect(result.current.hasExternalUpdate).toBe(false);
  });

  it("clears on path change", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(true);

    rerender({ content: "v2", path: "/other.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// dismissExternalUpdate
// ---------------------------------------------------------------------------

describe("dismissExternalUpdate", () => {
  it("clears hasExternalUpdate without changing editorKey", () => {
    const { result, rerender } = renderSync({ content: "v1" });
    const keyBefore = result.current.editorKey;

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(true);

    act(() => {
      result.current.dismissExternalUpdate();
    });

    expect(result.current.hasExternalUpdate).toBe(false);
    // No remount — user chose to keep their edits.
    expect(result.current.editorKey).toBe(keyBefore);
  });

  it("keeps isDirty true after dismissing", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    act(() => {
      result.current.dismissExternalUpdate();
    });

    expect(result.current.isDirty).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// discardAndApplyExternal
// ---------------------------------------------------------------------------

describe("discardAndApplyExternal", () => {
  it("clears hasExternalUpdate and isDirty", () => {
    const { result, rerender } = renderSync({ content: "v1" });

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    expect(result.current.hasExternalUpdate).toBe(true);

    act(() => {
      result.current.discardAndApplyExternal();
    });

    expect(result.current.hasExternalUpdate).toBe(false);
    expect(result.current.isDirty).toBe(false);
  });

  it("increments editorKey to trigger a remount", () => {
    const { result, rerender } = renderSync({ content: "v1" });
    const keyBefore = result.current.editorKey;

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    act(() => {
      result.current.discardAndApplyExternal();
    });

    expect(result.current.editorKey).toBeGreaterThan(keyBefore);
  });

  it("calls onDirtyChange(false)", () => {
    const onDirtyChange = vi.fn();
    const { result, rerender } = renderHook(
      (props: { content: string; path: string; isSettled: boolean }) =>
        useMarkdownEditorSync({ ...props, onDirtyChange }),
      { initialProps: { content: "v1", path: "/file.md", isSettled: true } },
    );

    act(() => {
      result.current.setDirty(true);
    });
    rerender({ content: "v2", path: "/file.md", isSettled: true });
    onDirtyChange.mockClear();

    act(() => {
      result.current.discardAndApplyExternal();
    });

    expect(onDirtyChange).toHaveBeenCalledWith(false);
  });
});
