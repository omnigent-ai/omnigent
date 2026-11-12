import { StrictMode, useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ResponsiveViewerSlot } from "./ResponsiveViewerSlot";

afterEach(cleanup);

it("preserves the editor, draft, focus, and backward selection across layout changes", () => {
  function Draft() {
    const [draft, setDraft] = useState("");
    return (
      <>
        <textarea
          aria-label="Comment draft"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <div contentEditable suppressContentEditableWarning role="textbox">
          Code sample
        </div>
      </>
    );
  }
  const mobileTarget = document.createElement("div");
  document.body.appendChild(mobileTarget);
  const slot = (mobile: boolean) => (
    <StrictMode>
      <ResponsiveViewerSlot mobile={mobile} mobileTarget={mobileTarget}>
        <Draft />
      </ResponsiveViewerSlot>
    </StrictMode>
  );
  const { rerender, unmount } = render(slot(false));
  const draft = screen.getByLabelText("Comment draft");
  fireEvent.change(draft, { target: { value: "Unsaved comment" } });
  const editor = screen.getByRole("textbox", { name: "" });
  editor.focus();
  const text = editor.firstChild!;
  document.getSelection()!.setBaseAndExtent(text, 6, text, 1);

  for (const mobile of [true, false, true]) {
    rerender(slot(mobile));
    expect(screen.getByLabelText("Comment draft")).toBe(draft);
    expect(draft).toHaveValue("Unsaved comment");
    expect(screen.getByRole("textbox", { name: "" })).toBe(editor);
    expect(mobileTarget.contains(editor)).toBe(mobile);
    expect(editor).toHaveFocus();
    expect(document.getSelection()!.anchorOffset).toBe(6);
    expect(document.getSelection()!.focusOffset).toBe(1);
  }
  unmount();
  expect(mobileTarget).toBeEmptyDOMElement();
  mobileTarget.remove();
});

it("attaches an initially mobile viewer when its target becomes available", () => {
  const { rerender } = render(
    <ResponsiveViewerSlot mobile mobileTarget={null}>
      Viewer
    </ResponsiveViewerSlot>,
  );
  expect(screen.getByText("Viewer")).toBeInTheDocument();
  const mobileTarget = document.createElement("div");
  document.body.appendChild(mobileTarget);
  rerender(
    <ResponsiveViewerSlot mobile mobileTarget={mobileTarget}>
      Viewer
    </ResponsiveViewerSlot>,
  );
  expect(mobileTarget).toHaveTextContent("Viewer");
  cleanup();
  expect(mobileTarget).toBeEmptyDOMElement();
  mobileTarget.remove();
});

it.each(["hidden", "blurred", "elsewhere"] as const)(
  "restores only implicitly lost focus when the draft was %s before moving",
  (reason) => {
    const mobileTarget = document.createElement("div");
    document.body.appendChild(mobileTarget);
    const slot = (mobile: boolean) => (
      <>
        <button type="button">Outside viewer</button>
        <ResponsiveViewerSlot mobile={mobile} mobileTarget={mobileTarget}>
          <textarea aria-label="Comment draft" defaultValue="Unsaved comment" />
        </ResponsiveViewerSlot>
      </>
    );
    const { rerender, unmount } = render(slot(false));
    const draft = screen.getByLabelText<HTMLTextAreaElement>("Comment draft");
    draft.focus();
    draft.setSelectionRange(2, 7, "backward");
    // jsdom has no layout; model whether the old slot still has a CSS box.
    vi.spyOn(draft, "getClientRects").mockReturnValue(
      (reason === "blurred" ? [new DOMRect()] : []) as unknown as DOMRectList,
    );
    draft.blur();
    if (reason === "elsewhere") {
      const outside = screen.getByRole("button", { name: "Outside viewer" });
      outside.focus();
      outside.blur();
    }
    expect(document.body).toHaveFocus();
    rerender(slot(true));
    expect(mobileTarget).toContainElement(draft);
    expect(draft).toHaveValue("Unsaved comment");
    if (reason === "hidden") {
      expect(draft).toHaveFocus();
      expect([draft.selectionStart, draft.selectionEnd, draft.selectionDirection]).toEqual([
        2,
        7,
        "backward",
      ]);
    } else {
      expect(document.body).toHaveFocus();
    }
    unmount();
    mobileTarget.remove();
  },
);
