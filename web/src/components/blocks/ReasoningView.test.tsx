// DOM smoke for the RenderItem.reasoning -> <Reasoning> adapter. The
// adapter's one job beyond threading props is deciding whether a section
// is *expandable*: a settled section with no content must not render a
// dead expand affordance, while a streaming section (or any section with
// text) must stay expandable. Pure jsdom — no animation timing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ReasoningView } from "./ReasoningView";

afterEach(cleanup);

describe("ReasoningView — expandable gating", () => {
  it("a settled section with empty text is NOT expandable", () => {
    // This is the bug being fixed: a `reasoning_start` with no chunks
    // (text="") that is no longer streaming. It should render a flat
    // header, not a clickable collapsible. A failure here (a button is
    // present) means an empty thinking block still expands into nothing.
    render(<ReasoningView text="" isStreaming={false} duration={undefined} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a settled section with whitespace-only text is NOT expandable", () => {
    // text.trim() guards against a section whose only "content" is
    // newlines/spaces — equally nothing to show. If the .trim() were
    // dropped, "   \n  ".length > 0 is true, so `expandable` would
    // flip to true and a trigger button would render — failing this
    // assertion. That's the regression this test pins down.
    // NB: the value must be a JSX expression ({"..."}) so the \n is a
    // real newline; a double-quoted attribute would pass the literal
    // two chars backslash+n, which trim() does not strip.
    render(<ReasoningView text={"   \n  "} isStreaming={false} duration={undefined} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a streaming section with empty text IS expandable and shows the shimmer", () => {
    // While streaming, content is en route and the 'Thinking...' shimmer
    // is live feedback — keep the section expandable even before the
    // first chunk lands. A failure (no button) would mean we collapsed
    // the live edge of an in-progress thought. The shimmer assertion
    // proves the computed `expandable` (isStreaming || ...) actually
    // gates the interactive header here — not just the explicit
    // `expandable` prop path exercised in reasoning.test.tsx.
    render(<ReasoningView text="" isStreaming={true} duration={undefined} />);
    expect(screen.getByRole("button")).toBeTruthy();
    expect(screen.getByText("Thinking...")).toBeTruthy();
  });

  it("a settled section with content IS expandable and shows its text", () => {
    // The normal completed-thought case: expandable header that, when
    // clicked open, reveals the reasoning body. A settled section starts
    // collapsed and Radix unmounts the content while closed, so we click
    // the trigger to expand first. Asserting on the actual text after
    // expanding proves the content path still renders end-to-end, not
    // just that a button exists.
    render(
      <ReasoningView
        text={"Weighing the options.\n\nPicked the cheaper one."}
        isStreaming={false}
        duration={2.5}
      />,
    );
    const trigger = screen.getByRole("button");
    expect(trigger).toBeTruthy();
    expect(screen.queryByText("Picked the cheaper one.")).toBeNull(); // collapsed
    fireEvent.click(trigger);
    expect(screen.getByText("Picked the cheaper one.")).toBeTruthy(); // expanded
  });
});

describe("ReasoningView — header preview", () => {
  it("previews the first non-blank line of a settled thought instead of its duration", () => {
    render(
      <ReasoningView
        text={"\n  Checking the lockfile first.\nThen the build config."}
        isStreaming={false}
        duration={2.5}
      />,
    );
    expect(screen.getByRole("button")).toHaveTextContent(
      /^Thinking: Checking the lockfile first\.$/,
    );
    expect(screen.queryByText(/Thought for/)).toBeNull();
  });

  it("strips markdown markers from the preview line", () => {
    render(
      <ReasoningView
        text={"## **Planning** the `retry` fix\n\nDetails."}
        isStreaming={false}
        duration={undefined}
      />,
    );
    expect(screen.getByRole("button")).toHaveTextContent(/^Thinking: Planning the retry fix$/);
  });

  it("keeps dunder names intact in the preview", () => {
    render(
      <ReasoningView
        text="Checking __init__.py and `__main__` first."
        isStreaming={false}
        duration={undefined}
      />,
    );
    expect(screen.getByRole("button")).toHaveTextContent(
      /^Thinking: Checking __init__\.py and __main__ first\.$/,
    );
  });

  it("skips code fence and thematic break lines when choosing the preview", () => {
    render(
      <ReasoningView
        text={"---\n```python\nimport os\n```"}
        isStreaming={false}
        duration={undefined}
      />,
    );
    expect(screen.getByRole("button")).toHaveTextContent(/^Thinking: import os$/);
  });

  it("keeps the shimmer while streaming rather than previewing partial text", () => {
    render(<ReasoningView text="Checking the lockfile" isStreaming={true} duration={undefined} />);
    expect(screen.getByText("Thinking...")).toBeTruthy();
    expect(screen.queryByText(/Thinking:/)).toBeNull();
  });

  it("falls back to the duration label when a settled thought has no text", () => {
    render(<ReasoningView text="" isStreaming={false} duration={3} />);
    expect(screen.getByText("Thought for 3.0 seconds")).toBeTruthy();
  });
});
