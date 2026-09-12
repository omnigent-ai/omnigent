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
