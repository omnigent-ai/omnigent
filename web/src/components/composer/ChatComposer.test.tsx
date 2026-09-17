import { createRef, type FormEvent, type ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ChatComposer,
  ComposerTextarea,
  ComposerSendButton,
  COMPOSER_LABELS_MIN_GAP_PX,
} from "./ChatComposer";

describe("ChatComposer", () => {
  it("keeps route-owned input and submit handlers on the shared surface", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    const inputRef = createRef<HTMLTextAreaElement>();
    render(
      <form onSubmit={onSubmit}>
        <ChatComposer
          data-testid="shared-composer"
          keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
          input={{ ref: inputRef, "aria-label": "Message", onChange }}
          actions={{
            leading: <span>Context controls</span>,
            trailing: <ComposerSendButton label="Send" />,
          }}
        />
      </form>,
    );
    expect(screen.getByTestId("shared-composer")).toHaveAttribute("data-composer-card");
    expect(inputRef.current).toBe(screen.getByRole("textbox"));
    expect(inputRef.current?.parentElement).toHaveClass("relative", "overflow-hidden");
    expect(screen.getByText("Context controls").parentElement?.parentElement).toHaveClass(
      "@container/composer-actions",
    );
    fireEvent.change(inputRef.current!, { target: { value: "Keep this draft" } });
    expect(onChange).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("scopes responsive draft typography to the input area, not the toolbar", () => {
    render(
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Draft" }}
        actions={{ leading: <span>Composer actions</span>, trailing: null }}
      />,
    );
    const input = screen.getByRole("textbox");
    expect(input).toHaveClass("composer-input-text", "text-ui");
    expect(input.parentElement).toHaveClass("composer-input-text", "text-ui");
    expect(input).not.toHaveClass("text-[13px]", "leading-[20.8px]");
    expect(screen.getByText("Composer actions").closest(".composer-input-text")).toBeNull();
  });

  it("preserves interrupt and pending-creation states", () => {
    const { rerender } = render(<ComposerSendButton label="Interrupt" interrupt />);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
    rerender(<ComposerSendButton label="Starting session" busy disabled />);
    expect(screen.getByRole("button", { name: "Starting session" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Starting session" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("places context, overlays, attachments and controls around the same input", () => {
    const cardRef = createRef<HTMLDivElement>();
    render(
      <ChatComposer
        ref={cardRef}
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Message", disabled: true }}
        slots={{
          beforeInput: <span>Quote</span>,
          inputBackdrop: <span>Highlight</span>,
          inputHint: <span>Skills</span>,
          attachments: <span>Attachment</span>,
        }}
        actions={{
          leading: <span>Add</span>,
          trailing: <ComposerSendButton label="Send" disabled />,
        }}
      />,
    );
    const input = screen.getByRole("textbox");
    const inputArea = input.parentElement!;
    expect(Array.from(inputArea.children)).toEqual([
      screen.getByText("Highlight"),
      input,
      screen.getByText("Skills"),
    ]);
    expect(Array.from(cardRef.current!.children)).toEqual([
      screen.getByText("Quote"),
      inputArea,
      screen.getByText("Attachment"),
      screen.getByText("Add").parentElement!.parentElement,
    ]);
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("filters composition keys before invoking controller keyboard behavior", () => {
    const onKeyDown = vi.fn();
    render(<ComposerTextarea aria-label="Draft" onKeyDown={onKeyDown} />);
    const input = screen.getByRole("textbox");
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onKeyDown).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).toHaveBeenCalledOnce();
  });

  it("shares send intent and touch newline precedence without submitting for the controller", () => {
    const onKeyDown = vi.fn();
    const props = {
      input: { "aria-label": "Draft", onKeyDown },
      actions: { leading: null, trailing: null },
    };
    const { rerender } = render(
      <ChatComposer
        {...props}
        keyboard={{ submitWithModEnter: true, preventsKeyboardSubmit: false }}
      />,
    );
    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: false,
      shouldPreferSendOverCompletion: false,
    });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: true,
      shouldPreferSendOverCompletion: true,
    });
    onKeyDown.mockClear();
    rerender(
      <ChatComposer
        {...props}
        keyboard={{ submitWithModEnter: true, preventsKeyboardSubmit: true }}
      />,
    );
    expect(fireEvent.keyDown(input, { key: "Enter", ctrlKey: true })).toBe(true);
    expect(onKeyDown).not.toHaveBeenCalled();
  });
});

describe("ChatComposer label collapse", () => {
  class StubResizeObserver {
    static callbacks: ResizeObserverCallback[] = [];
    constructor(callback: ResizeObserverCallback) {
      StubResizeObserver.callbacks.push(callback);
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  const fireResize = () => {
    for (const callback of StubResizeObserver.callbacks) callback([], {} as ResizeObserver);
  };
  // Width of a group once its label is hidden and only the icon remains.
  const ICON_WIDTH = 40;
  const ROW_PADDING = 16;

  afterEach(() => {
    StubResizeObserver.callbacks = [];
    vi.unstubAllGlobals();
  });

  /**
   * jsdom does no layout, so stand in for it: the row is `layout.rowWidth`
   * wide with 8px side padding, and each group's natural width follows the
   * text inside it (10px per character) — except that, exactly like the real
   * CSS, a collapsed row hides the labels and leaves each group its icon width.
   */
  function renderMeasuredComposer(actions: { leading: ReactNode; trailing: ReactNode }) {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    const layout = { rowWidth: 0 };
    const composer = (next: typeof actions) => (
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Message" }}
        actions={{ ...next, testId: "row", leadingTestId: "leading", trailingTestId: "trailing" }}
      />
    );
    const view = render(composer(actions));
    const row = screen.getByTestId("row");
    row.style.paddingLeft = "8px";
    row.style.paddingRight = "8px";
    Object.defineProperty(row, "clientWidth", { configurable: true, get: () => layout.rowWidth });
    for (const group of [screen.getByTestId("leading"), screen.getByTestId("trailing")]) {
      Object.defineProperty(group, "scrollWidth", {
        configurable: true,
        get: () =>
          row.dataset.labels === "collapsed" ? ICON_WIDTH : (group.textContent?.length ?? 0) * 10,
      });
    }
    return { row, layout, rerender: (next: typeof actions) => view.rerender(composer(next)) };
  }

  it("collapses the labels to icons once both groups stop fitting on one line with a gap", () => {
    const { row, layout } = renderMeasuredComposer({
      leading: <span>Bypass permissions</span>,
      trailing: <span>Fable 5.1 xHigh</span>,
    });
    const needed = 180 + 150 + COMPOSER_LABELS_MIN_GAP_PX;
    layout.rowWidth = needed + ROW_PADDING;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
    layout.rowWidth = needed + ROW_PADDING - 1;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
  });

  it("judges the expanded labels while collapsed, so icon-width room never brings them back", () => {
    const { row, layout } = renderMeasuredComposer({
      leading: <span>Bypass permissions</span>,
      trailing: <span>Fable 5.1 xHigh</span>,
    });
    layout.rowWidth = 200;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
    // Plenty of room for two icons (2 × 40 + 24), none for the labels (354).
    layout.rowWidth = 300;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
    layout.rowWidth = 180 + 150 + COMPOSER_LABELS_MIN_GAP_PX + ROW_PADDING;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
  });

  it("re-measures when the controls inside the row change", async () => {
    const { row, layout, rerender } = renderMeasuredComposer({
      leading: <span>Manual</span>,
      trailing: <span>Sonnet 5</span>,
    });
    layout.rowWidth = 300;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
    rerender({ leading: <span>Manual</span>, trailing: <span>Fable 5.1 (1M context) xHigh</span> });
    await waitFor(() => expect(row).toHaveAttribute("data-labels", "collapsed"));
    rerender({ leading: <span>Manual</span>, trailing: <span>Sonnet 5</span> });
    await waitFor(() => expect(row).not.toHaveAttribute("data-labels"));
  });
});
