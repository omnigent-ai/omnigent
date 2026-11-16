import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, createEvent, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { FileViewerContext } from "@/shell/FileViewerContext";

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
});

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

/** Renders an agent reply the way the chat transcript does. */
async function renderReply(markdown: string): Promise<HTMLElement> {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={[{ kind: "text", itemId: "t1", text: markdown, final: true }]}
            sessionStatus="idle"
          />
        </FileViewerContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // Code highlighting resolves after the first paint.
  await act(async () => {});
  return screen.getByTestId("assistant-text-section");
}

function textNodeContaining(root: Element, text: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if ((node as Text).data.includes(text)) return node as Text;
  }
  throw new Error(`no text node contains ${JSON.stringify(text)}`);
}

/** Selects from the start of `startText` to the end of `endText`. */
function select(root: Element, startText: string, endText: string): void {
  const range = document.createRange();
  const start = textNodeContaining(root, startText);
  const end = textNodeContaining(root, endText);
  range.setStart(start, start.data.indexOf(startText));
  range.setEnd(end, end.data.lastIndexOf(endText) + endText.length);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
}

/** Fires Cmd/Ctrl+C's copy event on `target` and returns what was written. */
function copy(target: Element): { flavors: Record<string, string>; prevented: boolean } {
  const flavors: Record<string, string> = {};
  const event = createEvent.copy(target);
  Object.defineProperty(event, "clipboardData", {
    value: { setData: (type: string, value: string) => (flavors[type] = value) },
  });
  fireEvent(target, event);
  return { flavors, prevented: event.defaultPrevented };
}

describe("copying a selection from an agent reply", () => {
  it("keeps a link inside a selected sentence", async () => {
    const section = await renderReply(
      "Intro.\n\nRead the [setup guide](https://example.com/setup) before you start. Outro.",
    );
    select(section, "Read", "start.");

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(true);
    expect(flavors["text/plain"]).toBe(
      "Read the [setup guide](<https://example.com/setup>) before you start.",
    );
    expect(flavors["text/html"]).toBe(
      'Read the <a href="https://example.com/setup">setup guide</a> before you start.',
    );
  });

  it("keeps the link when only part of its text is selected", async () => {
    const section = await renderReply("See [the setup guide](https://example.com/setup).");
    select(section, "setup", "setup");

    const { flavors } = copy(section);

    expect(flavors["text/plain"]).toBe("[setup](<https://example.com/setup>)");
    expect(flavors["text/html"]).toBe('<a href="https://example.com/setup">setup</a>');
  });

  it("keeps block structure and formatting across paragraphs and lists", async () => {
    const section = await renderReply(
      "First **bold** with [docs](https://example.com/docs).\n\n- item [mail](mailto:team@example.com)\n- other",
    );
    select(section, "First", "other");

    const { flavors } = copy(section);

    // Whitespace between block elements renders as nothing.
    expect(flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(
      '<p>First <strong>bold</strong> with <a href="https://example.com/docs">docs</a>.</p>' +
        '<ul><li>item <a href="mailto:team@example.com">mail</a></li><li>other</li></ul>',
    );
    expect(flavors["text/plain"]).toContain("First bold with [docs](<https://example.com/docs>).");
    expect(flavors["text/plain"]).toContain("item [mail](<mailto:team@example.com>)");
  });

  it("leaves a bare URL as typed and escapes markdown in link text", async () => {
    const section = await renderReply(
      "Open https://example.com/a, https://example.com or [a [b] c](https://example.com/b?q=<x>) now.",
    );
    select(section, "Open", "now.");

    const { flavors } = copy(section);

    expect(flavors["text/plain"]).toBe(
      "Open https://example.com/a, https://example.com or [a \\[b\\] c](<https://example.com/b?q=%3Cx%3E>) now.",
    );
  });

  it("drops links that only resolve inside the app, keeping their text", async () => {
    const section = await renderReply(
      "Back to [the chat](/c/abc) or [the site](https://example.com).",
    );
    select(section, "Back", "site");

    const { flavors } = copy(section);

    expect(flavors["text/html"]).toBe(
      'Back to the chat or <a href="https://example.com/">the site</a>',
    );
    expect(flavors["text/plain"]).toBe("Back to the chat or [the site](<https://example.com/>)");
  });

  it("leaves code block controls out of the copied HTML", async () => {
    const section = await renderReply("See [docs](https://example.com).\n\n```sh\necho hi\n```");
    select(section, "See", "echo hi");

    const { flavors } = copy(section);

    expect(flavors["text/html"]).toContain('<a href="https://example.com/">docs</a>');
    expect(flavors["text/html"]).toContain("echo hi");
    expect(flavors["text/html"]).not.toMatch(/<(button|svg|div|span)\b/);
  });

  it("keeps the browser's copy for a selection without a pasteable link", async () => {
    const section = await renderReply("Plain **prose** and [a file](src/App.tsx) here.");
    select(section, "Plain", "here.");

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
  });

  it("keeps the browser's copy for a selection that runs past the reply", async () => {
    const section = await renderReply("Read [docs](https://example.com) first.");
    const nextMessage = document.body.appendChild(document.createElement("p"));
    nextMessage.textContent = "Next message";
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "Read"), 0);
    range.setEnd(nextMessage.firstChild!, 4);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    const { flavors, prevented } = copy(section);
    nextMessage.remove();

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
  });
});
