import { cleanup, render, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodeBlockGuardedPre } from "./code-block-boundary";
import { MessageResponse } from "./message";
import { Reasoning, ReasoningContent, ReasoningTrigger } from "./reasoning";

afterEach(cleanup);

const FALLBACK_TEXT = "Could not render this markdown.";

// Streamdown renders the highlighted code body and mermaid diagrams behind
// React.lazy chunks; a chunk fetch that fails once is re-thrown on every
// render. Simulated here with code components that throw.
const Boom = () => {
  throw new Error("chunk fetch failed");
};

// Carries the className/children a real code element would, so the boundary
// can recover the language and source from the failing block.
const BoomWithSource = (_props: { className?: string; children?: ReactNode }): ReactNode => {
  throw new Error("chunk fetch failed");
};

describe("a render fault inside a code block stays contained to that block", () => {
  const MARKDOWN = "before the block\n\n```ts\nconst answer = 42;\n```\n\nafter the block";

  it("degrades the failing block to plain source and keeps the message rendered", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { container } = render(
        <MessageResponse components={{ code: Boom }}>{MARKDOWN}</MessageResponse>,
      );

      // The whole-message fallback must not swallow the message.
      expect(container.textContent).not.toContain(FALLBACK_TEXT);

      // The failing block degrades to a plain code block with its source.
      const body = container.querySelector('[data-streamdown="code-block-body"]');
      expect(body).toBeTruthy();
      expect(body?.textContent).toContain("const answer = 42;");

      // Surrounding prose still renders as markdown, not as raw source.
      const paragraphs = [...container.querySelectorAll("p")].map((p) => p.textContent);
      expect(paragraphs).toContain("before the block");
      expect(paragraphs).toContain("after the block");
    } finally {
      errors.mockRestore();
    }
  });

  it("labels the degraded block with the fence's language", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { container } = render(
        <CodeBlockGuardedPre>
          <BoomWithSource className="language-python">{'print("hi")\n'}</BoomWithSource>
        </CodeBlockGuardedPre>,
      );

      const block = container.querySelector('[data-streamdown="code-block"]');
      expect(block?.getAttribute("data-language")).toBe("python");
      expect(container.querySelector('[data-streamdown="code-block-body"]')?.textContent).toContain(
        'print("hi")',
      );
      expect(container.textContent).not.toContain(FALLBACK_TEXT);
    } finally {
      errors.mockRestore();
    }
  });

  it("renders a healthy code block through the guarded pre unchanged", async () => {
    const { container } = render(
      <Reasoning isStreaming={true}>
        <ReasoningTrigger />
        <ReasoningContent>{"```ts\nconst x = 1;\n```"}</ReasoningContent>
      </Reasoning>,
    );

    // The block path (not inline-code) proves the default pre's data-block
    // tagging is preserved by the guarded override.
    await waitFor(() =>
      expect(container.querySelector('[data-streamdown="code-block"]')).not.toBeNull(),
    );
    expect(container.querySelector('[data-streamdown="inline-code"]')).toBeNull();
    expect(container.textContent).toContain("const x = 1;");
  });
});
