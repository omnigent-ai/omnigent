import { createRef, type FormEvent } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatComposer, ComposerTextarea, ComposerSendButton } from "./ChatComposer";

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

  it("uses the settings-driven chat typography for the draft and input area", () => {
    render(
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Draft" }}
        actions={{ leading: null, trailing: null }}
      />,
    );
    const input = screen.getByRole("textbox");
    expect(input).toHaveClass("text-ui");
    expect(input.parentElement).toHaveClass("text-ui");
    expect(input).not.toHaveClass("text-[13px]", "leading-[20.8px]");
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

  it("keeps both action clusters on one flex line at narrow widths", () => {
    render(
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Message" }}
        actions={{
          leading: <span>Left cluster</span>,
          trailing: <span>Right cluster</span>,
          testId: "action-row",
        }}
      />,
    );
    // The row must never wrap: a wrapped row drops the model/mic/send cluster
    // onto a second line below the left-hand controls when the column narrows.
    const row = screen.getByTestId("action-row");
    expect(row).toHaveClass("flex", "items-center", "justify-between");
    expect(row).not.toHaveClass("flex-wrap");
    // The trailing group absorbs the squeeze by shrinking (its pill labels
    // wrap or truncate) instead of holding its width and forcing a wrap.
    const trailing = screen.getByText("Right cluster").parentElement;
    expect(trailing).toHaveClass("min-w-0", "max-w-full", "ml-auto");
    expect(trailing).not.toHaveClass("shrink-0");
    // The leading group floors at its content width (min-width:auto), so its
    // rigid controls never overflow underneath the trailing cluster.
    const leading = screen.getByText("Left cluster").parentElement;
    expect(leading).toHaveClass("flex-auto");
    expect(leading).not.toHaveClass("min-w-0");
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
