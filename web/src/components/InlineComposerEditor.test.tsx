import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { InlineComposerEditor, type InlineComposerEditorHandle } from "./InlineComposerEditor";
import type { ComposerDraftPart } from "@/lib/composerContent";

function image(name: string): File {
  return new File([name], name, { type: "image/png" });
}

describe("InlineComposerEditor", () => {
  afterEach(cleanup);

  it("exposes the placeholder on the editable element", () => {
    const { getByPlaceholderText } = render(
      <InlineComposerEditor
        initialParts={[]}
        onChange={() => {}}
        ariaLabel="Message"
        placeholder="Send a message…"
      />,
    );

    expect(getByPlaceholderText("Send a message…")).toHaveAttribute("contenteditable", "true");
  });

  it("inserts picked files at the visible caret", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    let latest: ComposerDraftPart[] = [];
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[{ type: "text", text: "before after" }]}
        onChange={(parts) => {
          latest = parts;
        }}
        ariaLabel="Message"
      />,
    );

    act(() => {
      ref.current?.setSelection(7);
      ref.current?.insertFiles([image("middle.png")]);
    });

    expect(getByRole("textbox", { name: "Message" })).toBeTruthy();
    expect(latest.map((part) => (part.type === "text" ? part.text : part.file.name))).toEqual([
      "before ",
      "middle.png",
      "after",
    ]);
  });

  it("inserts a pasted image at the visible caret", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    let latest: ComposerDraftPart[] = [];
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[{ type: "text", text: "before after" }]}
        onChange={(parts) => {
          latest = parts;
        }}
        ariaLabel="Message"
      />,
    );
    const editor = getByRole("textbox", { name: "Message" });

    act(() => ref.current?.setSelection(7));
    fireEvent.paste(editor, {
      clipboardData: {
        files: [image("paste.png")],
        items: [],
        types: ["Files"],
        getData: () => "",
      },
    });

    expect(latest.map((part) => (part.type === "text" ? part.text : part.file.name))).toEqual([
      "before ",
      "paste.png",
      "after",
    ]);
  });

  it("moves a selected attachment with Alt+Arrow while preserving its identity", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    const shot = image("shot.png");
    let latest: ComposerDraftPart[] = [];
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[
          { type: "text", text: "ab" },
          { type: "attachment", file: shot },
          { type: "text", text: "cd" },
        ]}
        onChange={(parts) => {
          latest = parts;
        }}
        ariaLabel="Message"
      />,
    );

    act(() => ref.current?.selectAttachment(0));
    fireEvent.keyDown(getByRole("textbox", { name: "Message" }), {
      key: "ArrowLeft",
      altKey: true,
    });

    expect(latest.map((part) => (part.type === "text" ? part.text : part.file.name))).toEqual([
      "a",
      "shot.png",
      "bcd",
    ]);
  });

  it("moves a selected attachment across a paragraph boundary", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    const shot = image("shot.png");
    let latest: ComposerDraftPart[] = [];
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[
          { type: "text", text: "first\n" },
          { type: "attachment", file: shot },
          { type: "text", text: "second" },
        ]}
        onChange={(parts) => {
          latest = parts;
        }}
        ariaLabel="Message"
      />,
    );

    act(() => ref.current?.selectAttachment(0));
    fireEvent.keyDown(getByRole("textbox", { name: "Message" }), {
      key: "ArrowLeft",
      altKey: true,
    });

    expect(latest.map((part) => (part.type === "text" ? part.text : part.file.name))).toEqual([
      "first",
      "shot.png",
      "\nsecond",
    ]);
  });

  it("round-trips Shift+Enter as a newline", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[{ type: "text", text: "first" }]}
        onChange={() => {}}
        ariaLabel="Message"
      />,
    );
    const editor = getByRole("textbox", { name: "Message" });

    act(() => ref.current?.setSelection(5));
    fireEvent.keyDown(editor, { key: "Enter", shiftKey: true });

    expect(ref.current?.getProjection()).toBe("first\n");
  });

  it("does not add programmatic draft replacement to the undo history", () => {
    const ref = createRef<InlineComposerEditorHandle>();
    const { getByRole } = render(
      <InlineComposerEditor
        ref={ref}
        initialParts={[{ type: "text", text: "old session" }]}
        onChange={() => {}}
        ariaLabel="Message"
      />,
    );

    act(() => ref.current?.setParts([{ type: "text", text: "new session" }]));
    fireEvent.keyDown(getByRole("textbox", { name: "Message" }), {
      key: "z",
      ctrlKey: true,
    });

    expect(ref.current?.getProjection()).toBe("new session");
  });
});
