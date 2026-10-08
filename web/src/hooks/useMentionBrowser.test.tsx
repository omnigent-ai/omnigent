// Browsers drop a textarea's undo history when script assigns ``.value`` (React's
// controlled update), so the token swap must go through the editing command and
// leave that update a no-op. jsdom has no undo stack; these pin the mechanism.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { detectMentionAt, type MentionState } from "@/lib/composerMentions";
import { installEditingCommandStub, removeEditingCommandStub } from "@/test/editingCommandStub";
import { useMentionBrowser } from "./useMentionBrowser";

const REPORT: WorkspaceFile = {
  path: "report.md",
  name: "report.md",
  type: "file",
  bytes: 10,
  modified_at: null,
};
const SRC: WorkspaceFile = {
  path: "src",
  name: "src",
  type: "directory",
  bytes: null,
  modified_at: null,
};

/** Minimal controlled composer: the hook's two real hosts wire it the same way. */
function Harness({ entries }: { entries: WorkspaceFile[] }) {
  const [text, setText] = useState("");
  const [mention, setMention] = useState<MentionState | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const browser = useMentionBrowser({
    mention,
    setMention,
    mentionEntries: mention ? entries : [],
    text,
    setText,
    textareaRef,
  });
  return (
    <>
      <textarea
        aria-label="draft"
        ref={textareaRef}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setMention(
            detectMentionAt(e.target.value, e.target.selectionStart ?? e.target.value.length),
          );
        }}
        onKeyDown={(e) => browser.handleKeyDown(e)}
      />
      <ul>
        {browser.mentionedItems.map((item) => (
          <li key={item.path}>{item.isDir ? `${item.path}/` : item.path}</li>
        ))}
      </ul>
      <output data-testid="token">{mention ? mention.query : "none"}</output>
    </>
  );
}

function draft() {
  return screen.getByLabelText("draft") as HTMLTextAreaElement;
}

/** Type ``text`` with the caret at its end, as a user would. */
function type(text: string) {
  const ta = draft();
  ta.focus();
  fireEvent.change(ta, { target: { value: text, selectionStart: text.length } });
}

describe("useMentionBrowser token replacement", () => {
  afterEach(() => {
    cleanup();
    removeEditingCommandStub();
    vi.restoreAllMocks();
  });

  it("attaches by deleting the token through the editing command, not a value rewrite", async () => {
    const execCommand = installEditingCommandStub();
    const valueSetter = vi.spyOn(HTMLTextAreaElement.prototype, "value", "set");
    render(<Harness entries={[REPORT]} />);
    type("alpha bravo @rep");
    valueSetter.mockClear();

    fireEvent.keyDown(draft(), { key: "Tab" });

    expect(execCommand).toHaveBeenCalledWith("delete");
    expect(draft().value).toBe("alpha bravo ");
    expect(screen.getByText("report.md")).toBeInTheDocument();
    expect(screen.getByTestId("token").textContent).toBe("none");
    // The DOM already held the new text, so the controlled update left it alone.
    expect(valueSetter).not.toHaveBeenCalled();
    await act(async () => {
      await Promise.resolve();
    });
    expect(draft().selectionStart).toBe("alpha bravo ".length);
  });

  it("drills into a folder by inserting the new token through the editing command", () => {
    const execCommand = installEditingCommandStub();
    const valueSetter = vi.spyOn(HTMLTextAreaElement.prototype, "value", "set");
    render(<Harness entries={[SRC]} />);
    type("see @sr");
    valueSetter.mockClear();

    fireEvent.keyDown(draft(), { key: "Enter" });

    expect(execCommand).toHaveBeenCalledWith("insertText", false, "@src/");
    expect(draft().value).toBe("see @src/");
    expect(screen.getByTestId("token").textContent).toBe("src/");
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(valueSetter).not.toHaveBeenCalled();
  });

  it("falls back to the controlled rewrite when the editing command is unavailable", () => {
    expect(typeof document.execCommand).toBe("undefined");
    const valueSetter = vi.spyOn(HTMLTextAreaElement.prototype, "value", "set");
    render(<Harness entries={[REPORT]} />);
    type("alpha @rep");
    valueSetter.mockClear();

    fireEvent.keyDown(draft(), { key: "Tab" });

    expect(draft().value).toBe("alpha ");
    expect(screen.getByText("report.md")).toBeInTheDocument();
    expect(valueSetter).toHaveBeenCalledWith("alpha ");
  });

  it("falls back to the controlled rewrite when the editing command is refused", () => {
    const execCommand = installEditingCommandStub();
    execCommand.mockImplementation(() => false);
    const valueSetter = vi.spyOn(HTMLTextAreaElement.prototype, "value", "set");
    render(<Harness entries={[SRC]} />);
    type("@sr");
    valueSetter.mockClear();

    fireEvent.keyDown(draft(), { key: "Enter" });

    expect(execCommand).toHaveBeenCalledWith("insertText", false, "@src/");
    expect(draft().value).toBe("@src/");
    expect(screen.getByTestId("token").textContent).toBe("src/");
    expect(valueSetter).toHaveBeenCalledWith("@src/");
  });

  it("falls back to the controlled rewrite when the editing command throws", () => {
    const execCommand = installEditingCommandStub();
    execCommand.mockImplementation(() => {
      throw new Error("command refused");
    });
    const valueSetter = vi.spyOn(HTMLTextAreaElement.prototype, "value", "set");
    render(<Harness entries={[REPORT]} />);
    type("alpha @rep");
    valueSetter.mockClear();

    fireEvent.keyDown(draft(), { key: "Tab" });

    expect(execCommand).toHaveBeenCalledWith("delete");
    expect(draft().value).toBe("alpha ");
    expect(screen.getByText("report.md")).toBeInTheDocument();
    expect(valueSetter).toHaveBeenCalledWith("alpha ");
  });
});
