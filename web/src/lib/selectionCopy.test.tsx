import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, createEvent, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { selectionToLinkedPayload } from "@/lib/selectionCopy";
import { FileViewerContext } from "@/shell/FileViewerContext";

afterEach(() => {
  vi.restoreAllMocks();
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

    // Spaces selected on either side of the link survive in both flavors.
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "Read the "), "Read the".length);
    range.setEnd(textNodeContaining(section, " before"), 1);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    const spaced = copy(section).flavors;
    expect(spaced["text/plain"]).toBe(" [setup guide](<https://example.com/setup>) ");
    expect(spaced["text/html"]).toBe(' <a href="https://example.com/setup">setup guide</a> ');
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

  it("links the anchor, not earlier plain text with the same words", async () => {
    const section = await renderReply(
      "Read docs, then [docs](https://example.com/setup). Use mydoc or [doc](https://example.com/doc) and [docs page](https://example.com/page).",
    );

    select(section, "Read", "page");
    expect(copy(section).flavors["text/plain"]).toBe(
      "Read docs, then [docs](<https://example.com/setup>). Use mydoc or [doc](<https://example.com/doc>) and [docs page](<https://example.com/page>)",
    );

    // Ends inside the last link, whose selected part repeats earlier words.
    const range = document.createRange();
    const start = textNodeContaining(section, "mydoc");
    range.setStart(start, start.data.indexOf("mydoc"));
    range.setEnd(textNodeContaining(section, "docs page"), "docs".length);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    expect(copy(section).flavors["text/plain"]).toBe(
      "mydoc or [doc](<https://example.com/doc>) and [docs](<https://example.com/page>)",
    );

    // The label's words also start one character before the link.
    cleanup();
    const overlapping = await renderReply("an[ana](https://example.com)");
    select(overlapping, "an", "ana");
    expect(copy(overlapping).flavors["text/plain"]).toBe("an[ana](<https://example.com/>)");
  });

  it("keeps list and table containers around selected items", async () => {
    const section = await renderReply(
      "1. one\n2. two [guide](https://example.com/guide)\n3. three\n\n" +
        "| name | link |\n| --- | --- |\n| alpha | [x](https://example.com/x) |\n\n" +
        "> quoted [q](https://example.com/q)\n>\n> second",
    );

    select(section, "two", "three");
    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(
      '<ol start="2"><li>two <a href="https://example.com/guide">guide</a></li><li>three</li></ol>',
    );

    select(section, "alpha", "x");
    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(
      '<table><tbody><tr><td>alpha</td><td><a href="https://example.com/x">x</a></td></tr></tbody></table>',
    );

    select(section, "quoted", "second");
    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(
      '<blockquote><p>quoted <a href="https://example.com/q">q</a></p><p>second</p></blockquote>',
    );
  });

  it("numbers a partly selected list that the selection runs past", async () => {
    const section = await renderReply(
      "1. one\n2. two [guide](https://example.com/guide)\n3. three\n\nAfter.",
    );
    const expected =
      '<ol start="2"><li>two <a href="https://example.com/guide">guide</a></li><li>three</li></ol><p>After.</p>';
    select(section, "two", "After.");
    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(expected);

    // Starting at the very end of item 1 selects none of it.
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "one"), "one".length);
    range.setEnd(textNodeContaining(section, "After."), "After.".length);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(expected);
  });

  it("counts earlier words across block breaks the way the browser copies them", async () => {
    const section = await renderReply(
      "| a | b |\n| --- | --- |\n| foo | bar |\n\nSee [foo bar](https://example.com/fb).",
    );
    select(section, "a", "foo bar");
    // Browsers separate table cells with tabs, which Range.toString omits.
    vi.spyOn(Selection.prototype, "toString").mockReturnValue("a\tb\nfoo\tbar\n\nSee foo bar");

    expect(copy(section).flavors["text/plain"]).toBe(
      "a\tb\nfoo\tbar\n\nSee [foo bar](<https://example.com/fb>)",
    );
  });

  it("places links by position when the browser renders line breaks", async () => {
    const section = await renderReply(
      "[one<br>two](https://example.com/one) then [onetwo](https://example.com/two)",
    );
    select(section, "one", "onetwo");
    // The browser copies <br> as a newline, which Range text leaves out.
    vi.spyOn(Selection.prototype, "toString").mockReturnValue("one\ntwo then onetwo");

    expect(copy(section).flavors["text/plain"]).toBe(
      "[one\ntwo](<https://example.com/one>) then [onetwo](<https://example.com/two>)",
    );
  });

  it("keeps the plain text as copied when it differs from the reply's text", async () => {
    const section = await renderReply(
      "Use [alpha](https://example.com/a) then [beta](https://example.com/b).",
    );
    select(section, "Use", "beta");
    vi.spyOn(Selection.prototype, "toString").mockReturnValue("Use alpha, then beta");

    const { flavors } = copy(section);

    expect(flavors["text/plain"]).toBe("Use alpha, then beta");
    expect(flavors["text/html"]).toContain('<a href="https://example.com/b">beta</a>');
  });

  it("ignores a link the selection only touches at its edge", async () => {
    const section = await renderReply("First paragraph.\n\n[Next](https://example.com) paragraph.");
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "First"), 0);
    range.setEnd(textNodeContaining(section, "Next"), 0);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
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
    // Highlighting can split "echo hi" across token spans.
    select(section, "See", "hi");

    const { flavors } = copy(section);

    expect(flavors["text/html"]).toContain('<a href="https://example.com/">docs</a>');
    expect(flavors["text/html"]).toContain("echo hi");
    expect(flavors["text/html"]).not.toMatch(/<(button|svg|div|span)\b/);
  });

  it("pastes a formula once, as its TeX source", async () => {
    const section = await renderReply("Area is $$x^2$$ per [docs](https://example.com).");
    expect(section.querySelector(".katex")).not.toBeNull();
    select(section, "Area", "docs");

    const html = copy(section).flavors["text/html"]!;

    expect(html).toBe('Area is x^2 per <a href="https://example.com/">docs</a>');
  });

  it("keeps button and image text but not hidden text in the HTML", () => {
    const root = document.body.appendChild(document.createElement("div"));
    root.innerHTML =
      '<p>Open <button type="button"><svg></svg><span>notes.md</span></button>, see ' +
      '<img alt="the diagram" src="https://example.com/d.png">' +
      '<span style="display: none">hidden</span> and <a href="https://example.com">docs</a></p>';
    const range = document.createRange();
    range.selectNodeContents(root);

    const payload = selectionToLinkedPayload(range, root, "Open notes.md, see  and docs");
    root.remove();

    expect(payload?.html).toBe(
      '<p>Open notes.md, see the diagram and <a href="https://example.com">docs</a></p>',
    );
    expect(payload?.text).toBe("Open notes.md, see  and [docs](<https://example.com>)");
  });

  it("copies only safe links and no attributes or scripts into the HTML", () => {
    const root = document.body.appendChild(document.createElement("div"));
    root.innerHTML =
      '<p style="color: red" onclick="alert(1)">Go <a href="javascript:alert(1)">bad</a>, ' +
      '<a href="https://example.com" onmouseover="alert(1)" class="x">good</a>' +
      "<script>alert(1)</script><iframe></iframe></p>";
    const range = document.createRange();
    range.selectNodeContents(root);

    const payload = selectionToLinkedPayload(range, root, "Go bad, good");
    root.remove();

    expect(payload?.html).toBe('<p>Go bad, <a href="https://example.com">good</a></p>');
  });

  it("keeps the browser's copy for a selection without a pasteable link", async () => {
    const section = await renderReply("Plain **prose** and [a file](src/App.tsx) here.");
    select(section, "Plain", "here.");

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
  });

  it("keeps the browser's copy for a multi-range selection", async () => {
    const section = await renderReply("Read [docs](https://example.com) first.");
    select(section, "Read", "first.");
    const selection = window.getSelection()!;
    vi.spyOn(selection, "rangeCount", "get").mockReturnValue(2);

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
  });

  it("keeps the browser's copy when writing the clipboard fails", async () => {
    const section = await renderReply("Read [docs](https://example.com) first.");
    select(section, "Read", "first.");
    const event = createEvent.copy(section);
    Object.defineProperty(event, "clipboardData", {
      value: {
        setData: (type: string) => {
          if (type === "text/html") throw new Error("write failed");
        },
      },
    });

    fireEvent(section, event);

    expect(event.defaultPrevented).toBe(false);
  });

  it("keeps the browser's copy when the conversion fails", async () => {
    const section = await renderReply("Read [docs](https://example.com) first.");
    select(section, "Read", "first.");
    vi.spyOn(Range.prototype, "intersectsNode").mockImplementation(() => {
      throw new Error("conversion failed");
    });

    const { flavors, prevented } = copy(section);

    expect(prevented).toBe(false);
    expect(flavors).toEqual({});
  });

  it("keeps links when the selection ends at the start of the next block", async () => {
    const section = await renderReply("Read [docs](https://example.com) first.");
    const nextMessage = document.body.appendChild(document.createElement("p"));
    nextMessage.textContent = "Next message";
    // Triple-clicking a reply's last paragraph can select up to here.
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "Read"), 0);
    range.setEnd(nextMessage.firstChild!, 0);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    const { flavors } = copy(section);
    nextMessage.remove();

    expect(flavors["text/plain"]).toBe("Read [docs](<https://example.com/>) first.");
    expect(flavors["text/html"]).toContain('<a href="https://example.com/">docs</a>');
  });

  it("leaves out blocks the selection only touches at its edges", async () => {
    const section = await renderReply("Before.\n\nRead [docs](https://example.com).\n\nAfter.");
    const range = document.createRange();
    range.setStart(textNodeContaining(section, "Before."), "Before.".length);
    range.setEnd(textNodeContaining(section, "After."), 0);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    expect(copy(section).flavors["text/html"]!.replace(/>\s+</g, "><")).toBe(
      '<p>Read <a href="https://example.com/">docs</a>.</p>',
    );
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
