// ⌘↵ in the pull request composer submits it and never accepts an agent request that is
// waiting, whatever the composer holds; outside the composer the chord still accepts.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const submitApproval = vi.fn();
let blocks: Record<string, unknown>[] = [];
vi.mock("@/store/chatStore", () => {
  const state = () => ({ blocks, submitApproval });
  const useChatStore = (select: (current: ReturnType<typeof state>) => unknown) => select(state());
  return { useChatStore: Object.assign(useChatStore, { getState: state }) };
});

import { TooltipProvider } from "@/components/ui/tooltip";
import { useApproveHotkey } from "@/hooks/useApproveHotkey";

import { MarkdownComposer } from "./MarkdownComposer";

// The PR editors' completion reads go through react-query, as in the app.
const queryClient = new QueryClient();

beforeEach(() => {
  submitApproval.mockClear();
  onSubmit.mockClear();
  blocks = [{ type: "elicitation", elicitationId: "e1", status: "pending" }];
});
afterEach(() => {
  cleanup();
  blocks = [];
});

const onSubmit = vi.fn();

function Host({ markdown, disabled = false }: { markdown: string; disabled?: boolean }) {
  // jsdom isn't a Mac, so the chord is Ctrl+Enter here.
  useApproveHotkey(false);
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <button type="button">Outside</button>
        <MarkdownComposer
          value={markdown}
          onChange={() => {}}
          onSubmit={onSubmit}
          submitLabel="Comment"
          submitVerb="comment"
          size="comment"
          label="Comment"
          disabled={disabled}
          preview={<p>{markdown}</p>}
          attachments={null}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function commandEnter(target: Element): void {
  act(() => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
}

describe("⌘↵ in the pull request composer with an agent request waiting", () => {
  it.each([
    ["empty", ""],
    ["whitespace only", "   \n\n  "],
    ["image only", "![shot](https://github.com/user-attachments/assets/1)"],
    ["with text", "Looks good."],
  ])("submits the %s composer and leaves the request alone", (_name, markdown) => {
    render(<Host markdown={markdown} />);
    const textarea = screen.getByRole("textbox", { name: "Comment" });
    textarea.focus();
    commandEnter(textarea);
    expect(submitApproval).not.toHaveBeenCalled();
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("leaves the request alone from a disabled composer", () => {
    render(<Host markdown="" disabled />);
    commandEnter(screen.getByRole("textbox", { name: "Comment" }));
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("submits from Preview and from a toolbar button without accepting the request", () => {
    render(<Host markdown="Ship it" />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    const preview = screen.getByRole("tabpanel", { name: "Preview" });
    preview.focus();
    commandEnter(preview);
    expect(submitApproval).not.toHaveBeenCalled();
    expect(onSubmit).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Write" }));
    const bold = screen.getByRole("button", { name: "Bold" });
    bold.focus();
    commandEnter(bold);
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("neither submits nor accepts the request from Preview while disabled", () => {
    render(<Host markdown="Ship it" disabled />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    const preview = screen.getByRole("tabpanel", { name: "Preview" });
    preview.focus();
    commandEnter(preview);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("still accepts the request from outside the composer", () => {
    render(<Host markdown="" />);
    const outside = screen.getByRole("button", { name: "Outside" });
    outside.focus();
    commandEnter(outside);
    expect(submitApproval).toHaveBeenCalledTimes(1);
  });
});
