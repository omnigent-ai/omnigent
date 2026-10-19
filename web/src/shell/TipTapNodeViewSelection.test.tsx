import { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  Editor,
  EditorContent,
  NodeViewContent,
  NodeViewWrapper,
  ReactNodeViewRenderer,
} from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { CodeBlock } from "@tiptap/extension-code-block";

function CodeView() {
  const [version, setVersion] = useState(0);
  return (
    <NodeViewWrapper>
      <button type="button" contentEditable={false} onClick={() => setVersion(version + 1)}>
        Move content
      </button>
      <NodeViewContent<"pre"> as="pre" />
      {version > 0 && <NodeViewContent<"pre"> as="pre" />}
    </NodeViewWrapper>
  );
}
const ReactCodeBlock = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(CodeView);
  },
});
let editor: Editor | undefined;
afterEach(() => {
  cleanup();
  editor?.destroy();
  editor = undefined;
  vi.restoreAllMocks();
});

async function mount(count: number, editable = true) {
  editor = new Editor({
    extensions: [StarterKit.configure({ codeBlock: false }), ReactCodeBlock],
    content: `<p>Heading</p>${"<pre><code>sample</code></pre>".repeat(count)}`,
    editable,
  });
  await act(async () => {
    render(<EditorContent editor={editor!} />);
  });
  return editor;
}

it("does not read DOM selection for each unselected React node view", async () => {
  const getSelection = vi.spyOn(document, "getSelection");
  await mount(100);
  expect(screen.getAllByRole("button", { name: "Move content" })).toHaveLength(100);
  // ProseMirror may read selection for the editor itself; the node count must
  // not produce one layout-triggering read per content DOM placement.
  expect(getSelection.mock.calls.length).toBeLessThan(10);
});

it.each([true, false])(
  "preserves selection when moving selected content (editable=%s)",
  async (editable) => {
    const instance = await mount(1, editable);
    const pos = instance.state.doc.child(0).nodeSize + 1;
    act(() => {
      instance.commands.setTextSelection({ from: pos + 1, to: pos + 4 });
    });
    const text = document
      .createTreeWalker(document.querySelector("[data-node-view-content]")!, NodeFilter.SHOW_TEXT)
      .nextNode();
    expect(text).toBeTruthy();
    document.getSelection()!.setBaseAndExtent(text!, 4, text!, 1);
    fireEvent.click(screen.getByRole("button", { name: "Move content" }));
    expect(document.getSelection()!.toString()).toBe("amp");
    expect(document.getSelection()!.anchorNode).toBe(text);
    expect(document.getSelection()!.anchorOffset).toBe(4);
    expect(document.getSelection()!.focusOffset).toBe(1);
  },
);
