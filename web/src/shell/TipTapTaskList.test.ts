/**
 * Unit tests for GitHub-style task lists (`- [ ]` / `- [x]`) in the markdown
 * editor.
 *
 * TaskList/TaskItem come from @tiptap/extension-list and ship their own
 * markdown tokenizer + parse/serialise handlers; MarkdownRichTextViewer just
 * registers them (StarterKit does not). These tests pin that wiring:
 *   - task syntax parses into a <ul data-type="taskList"> of checkbox items
 *   - checkbox state tracks `[ ]` vs `[x]`
 *   - the list round-trips back to identical markdown
 *   - plain bullet lists are NOT promoted to task lists
 *
 * Like the GitHub-alert tests, these mount a real TipTap Editor (real schema,
 * real @tiptap/markdown parsing) so a regression in either direction fails.
 */

import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import StarterKit from "@tiptap/starter-kit";

let editor: Editor | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
  host?.remove();
  host = null;
});

/** Mounted editor matching the viewer's task-list configuration. The host is
 *  attached to the document so commands that focus the editor (e.g. the
 *  checkbox-toggle node-view) can commit their transactions. */
function makeEditor(markdown: string): Editor {
  host = document.createElement("div");
  document.body.appendChild(host);
  return new Editor({
    element: host,
    extensions: [
      StarterKit.configure({ link: false, blockquote: false }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Markdown,
    ],
    content: markdown,
    contentType: "markdown",
  });
}

const TASK_MD = "- [ ] Buy milk\n- [x] Ship the PR";

describe("task lists", () => {
  it("parses `- [ ]` / `- [x]` into a checkbox list", () => {
    editor = makeEditor(TASK_MD);
    const list = editor.view.dom.querySelector('ul[data-type="taskList"]');
    expect(list).not.toBeNull();

    // TaskItem's node-view draws each item as a bare <li> (data-checked, no
    // data-type) inside the taskList <ul>, so match direct-child <li>s.
    const items = list!.querySelectorAll(":scope > li");
    expect(items).toHaveLength(2);
    expect(list!.textContent).toContain("Buy milk");
    expect(list!.textContent).toContain("Ship the PR");
  });

  it("reflects checked state on both the checkbox and data-checked", () => {
    editor = makeEditor(TASK_MD);
    // Node-view stamps data-checked (true/false) on each task <li>.
    const items = editor.view.dom.querySelectorAll("li[data-checked]");
    const boxes = editor.view.dom.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');

    expect(boxes).toHaveLength(2);
    expect(boxes[0].checked).toBe(false);
    expect(boxes[1].checked).toBe(true);
    expect(items[0].getAttribute("data-checked")).toBe("false");
    expect(items[1].getAttribute("data-checked")).toBe("true");
  });

  it("round-trips task-list markdown byte-faithfully", () => {
    editor = makeEditor(TASK_MD);
    expect(editor.getMarkdown().trim()).toBe(TASK_MD);
  });

  it("toggling a checkbox updates the marker and re-serializes to markdown", () => {
    editor = makeEditor(TASK_MD);
    const dom = editor.view.dom;
    const boxesOf = () => dom.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    const itemsOf = () => dom.querySelectorAll("li[data-checked]");

    // Click the first box ("Buy milk", unchecked). TaskItem's node-view listens
    // for the checkbox `change` event; jsdom's .click() flips `checked` and
    // dispatches it, mirroring a real user click.
    boxesOf()[0].click();

    expect(boxesOf()[0].checked).toBe(true);
    expect(itemsOf()[0].getAttribute("data-checked")).toBe("true");
    // The flip must reach the document, not just the DOM checkbox: re-serialize.
    expect(editor.getMarkdown().trim()).toBe("- [x] Buy milk\n- [x] Ship the PR");

    // Unchecking the second box ("Ship the PR") round-trips the other way.
    boxesOf()[1].click();

    expect(boxesOf()[1].checked).toBe(false);
    expect(itemsOf()[1].getAttribute("data-checked")).toBe("false");
    expect(editor.getMarkdown().trim()).toBe("- [x] Buy milk\n- [ ] Ship the PR");
  });

  it("leaves plain bullet lists as plain lists (no checkbox promotion)", () => {
    const md = "- apples\n- oranges";
    editor = makeEditor(md);
    // No task-list markup...
    expect(editor.view.dom.querySelector('ul[data-type="taskList"]')).toBeNull();
    expect(editor.view.dom.querySelector('input[type="checkbox"]')).toBeNull();
    // ...and a plain <ul> survives the round-trip.
    expect(editor.view.dom.querySelector("ul")).not.toBeNull();
    expect(editor.getMarkdown().trim()).toBe(md);
  });
});
