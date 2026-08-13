import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SelectionPopup } from "./SelectionPopup";
import {
  AskSubagentContextProvider,
  type AskSubagentContextValue,
} from "@/shell/AskSubagentContext";

const PARA = "Deletion vectors mark rows as deleted without rewriting files.";
const CODE_BLOCK = "fn foo() {\n  bar()\n}";
const LONG_PARAGRAPH = "x".repeat(2500);

// Fixture: a conversation container with two assistant responses (real block
// elements for excerpt extraction) and a non-assistant user message, plus
// content OUTSIDE the container (the composer).
function Fixture({
  onReply = vi.fn(),
  ask,
}: {
  onReply?: (text: string) => void;
  ask?: AskSubagentContextValue;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const popup = <SelectionPopup containerRef={ref} onReply={onReply} />;
  return (
    <>
      <div ref={ref} data-testid="container">
        <div data-testid="assistant-text-section">
          <p data-testid="asst-para">{PARA}</p>
          <pre data-testid="asst-code">{CODE_BLOCK}</pre>
          <p data-testid="asst-long">{LONG_PARAGRAPH}</p>
        </div>
        <div data-testid="assistant-text-section">
          <p data-testid="asst-para2">another response paragraph</p>
        </div>
        <div data-testid="user-msg">
          <p data-testid="usr">user typed this</p>
        </div>
      </div>
      <div data-testid="outside">
        <span data-testid="composer">composer text</span>
      </div>
      {ask ? <AskSubagentContextProvider value={ask}>{popup}</AskSubagentContextProvider> : popup}
    </>
  );
}

function textNode(testId: string): Node {
  const node = screen.getByTestId(testId).firstChild;
  if (node === null) throw new Error(`no text node in ${testId}`);
  return node;
}

function mockSelection(opts: { text: string; anchor: Node | null; focus?: Node | null }) {
  const rect = {
    left: 100,
    top: 100,
    right: 200,
    bottom: 120,
    width: 100,
    height: 20,
    x: 100,
    y: 100,
    toJSON: () => ({}),
  } as DOMRect;
  vi.spyOn(window, "getSelection").mockReturnValue({
    isCollapsed: false,
    rangeCount: 1,
    anchorNode: opts.anchor,
    focusNode: opts.focus ?? opts.anchor,
    toString: () => opts.text,
    getRangeAt: () =>
      ({
        getBoundingClientRect: () => rect,
        commonAncestorContainer: opts.anchor,
      }) as unknown as Range,
    removeAllRanges: vi.fn(),
  } as unknown as Selection);
}

const askCtx = (askSubagent = vi.fn()): AskSubagentContextValue => ({ canAsk: true, askSubagent });

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("SelectionPopup", () => {
  it("shows Reply for an in-container selection, without Ask outside an assistant response", () => {
    render(<Fixture ask={askCtx()} />);
    mockSelection({ text: "user typed this", anchor: textNode("usr") });
    fireEvent.mouseUp(document.body);
    expect(screen.getByTestId("selection-reply")).toBeInTheDocument();
    expect(screen.queryByTestId("selection-ask-subagent")).toBeNull();
  });

  it("shows Reply and Ask together in one toolbar for a selection inside one assistant response", () => {
    render(<Fixture ask={askCtx()} />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    const popup = screen.getByTestId("selection-popup");
    expect(within(popup).getByTestId("selection-reply")).toBeInTheDocument();
    expect(within(popup).getByTestId("selection-ask-subagent")).toBeInTheDocument();
  });

  it("Reply appends via onReply and never starts a sub-agent (stays in-agent)", () => {
    const onReply = vi.fn();
    const askSubagent = vi.fn();
    render(<Fixture onReply={onReply} ask={askCtx(askSubagent)} />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    fireEvent.click(screen.getByTestId("selection-reply"));
    expect(onReply).toHaveBeenCalledWith("Deletion vectors");
    // Reply is the in-agent quote/follow-up path — it must not create a child.
    expect(askSubagent).not.toHaveBeenCalled();
  });

  it("Ask captures the exact selection + nearest containing block as the excerpt", () => {
    const askSubagent = vi.fn();
    render(<Fixture ask={askCtx(askSubagent)} />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    fireEvent.click(screen.getByTestId("selection-ask-subagent"));
    expect(askSubagent).toHaveBeenCalledWith({
      selectedText: "Deletion vectors",
      surroundingExcerpt: PARA,
    });
  });

  it("omits the excerpt when the selection is the whole block", () => {
    const askSubagent = vi.fn();
    render(<Fixture ask={askCtx(askSubagent)} />);
    mockSelection({ text: PARA, anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    fireEvent.click(screen.getByTestId("selection-ask-subagent"));
    expect(askSubagent).toHaveBeenCalledWith({ selectedText: PARA, surroundingExcerpt: null });
  });

  it("keeps multi-line code-block excerpts intact", () => {
    const askSubagent = vi.fn();
    render(<Fixture ask={askCtx(askSubagent)} />);
    mockSelection({ text: "bar()", anchor: textNode("asst-code") });
    fireEvent.mouseUp(document.body);
    fireEvent.click(screen.getByTestId("selection-ask-subagent"));
    expect(askSubagent).toHaveBeenCalledWith({
      selectedText: "bar()",
      surroundingExcerpt: CODE_BLOCK,
    });
  });

  it("caps the surrounding excerpt at 2000 characters", () => {
    const askSubagent = vi.fn();
    render(<Fixture ask={askCtx(askSubagent)} />);
    mockSelection({ text: "x", anchor: textNode("asst-long") });
    fireEvent.mouseUp(document.body);
    fireEvent.click(screen.getByTestId("selection-ask-subagent"));
    const arg = askSubagent.mock.calls[0]![0] as { surroundingExcerpt: string | null };
    expect(arg.surroundingExcerpt).toHaveLength(2000);
  });

  it("hides Ask when the viewer lacks edit access (canAsk false)", () => {
    render(<Fixture ask={{ canAsk: false, askSubagent: vi.fn() }} />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    expect(screen.getByTestId("selection-reply")).toBeInTheDocument();
    expect(screen.queryByTestId("selection-ask-subagent")).toBeNull();
  });

  it("hides Ask when there is no AskSubagent provider", () => {
    render(<Fixture />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    expect(screen.getByTestId("selection-reply")).toBeInTheDocument();
    expect(screen.queryByTestId("selection-ask-subagent")).toBeNull();
  });

  it("hides Ask when the selection spans two assistant responses", () => {
    render(<Fixture ask={askCtx()} />);
    mockSelection({
      text: "spanning",
      anchor: textNode("asst-para"),
      focus: textNode("asst-para2"),
    });
    fireEvent.mouseUp(document.body);
    expect(screen.getByTestId("selection-reply")).toBeInTheDocument();
    expect(screen.queryByTestId("selection-ask-subagent")).toBeNull();
  });

  it("dismisses the toolbar when Escape is pressed", () => {
    render(<Fixture ask={askCtx()} />);
    mockSelection({ text: "Deletion vectors", anchor: textNode("asst-para") });
    fireEvent.mouseUp(document.body);
    expect(screen.getByTestId("selection-popup")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("selection-popup")).toBeNull();
  });

  it("shows no toolbar for a selection outside the conversation container", () => {
    render(<Fixture ask={askCtx()} />);
    mockSelection({ text: "composer text", anchor: textNode("composer") });
    fireEvent.mouseUp(document.body);
    expect(screen.queryByTestId("selection-popup")).toBeNull();
  });
});
