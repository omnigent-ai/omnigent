import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The file the workspace answers with, and what the viewer was asked to show.
const file = vi.hoisted(() => ({
  result: {} as {
    data?: { content: string; encoding: string; truncated?: boolean };
    isError?: boolean;
  },
  /** Each file read, in order. */
  read: [] as string[],
}));
vi.mock("@/hooks/useFileContent", () => ({
  useFileContent: (_sessionId: string, path: string) => {
    if (file.read.at(-1) !== path) file.read.push(path);
    return file.result;
  },
}));
vi.mock("../codeMirror/CodeMirrorFileViewer", () => ({
  CodeMirrorFileViewer: (props: { content: string; position?: { line: number } }) => (
    <pre data-line={props.position?.line}>{props.content}</pre>
  ),
}));

const { GoToFilePreview, PREVIEW_DEBOUNCE_MS } = await import("./GoToFilePreview");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  file.read = [];
});

describe("GoToFilePreview", () => {
  it("shows the file under a header naming it and its folder", () => {
    file.result = { data: { content: "print('hi')\n", encoding: "utf-8" } };
    render(<GoToFilePreview sessionId="conv" path="widgets/gadget.py" />);
    const preview = screen.getByTestId("go-to-file-preview");
    expect(preview).toHaveTextContent("gadget.py");
    expect(preview).toHaveTextContent("widgets");
    expect(preview).toHaveTextContent("print('hi')");
    expect(preview).not.toHaveTextContent(/Line \d/);
  });

  it("names the line it scrolls to when given one", () => {
    file.result = { data: { content: "a\nb\n", encoding: "utf-8" } };
    render(<GoToFilePreview sessionId="conv" path="a.ts" line={2} />);
    expect(screen.getByTestId("go-to-file-preview")).toHaveTextContent("Line 2");
    expect(document.querySelector("pre")).toHaveAttribute("data-line", "2");
  });

  it("says why it shows no file while loading, on failure and for binary files", () => {
    file.result = {};
    const { rerender } = render(<GoToFilePreview sessionId="conv" path="a.ts" />);
    expect(screen.getByText("Loading preview…")).toBeInTheDocument();
    file.result = { isError: true };
    rerender(<GoToFilePreview sessionId="conv" path="a.ts" />);
    expect(screen.getByText("Couldn't load a preview of this file.")).toBeInTheDocument();
    file.result = { data: { content: "", encoding: "base64" } };
    rerender(<GoToFilePreview sessionId="conv" path="a.png" />);
    expect(screen.getByText("This file can't be previewed as text.")).toBeInTheDocument();
    expect(screen.queryByTestId("go-to-file-preview")).toBeNull();
  });

  it("reads only the file the active row rests on, and the first one at once", () => {
    vi.useFakeTimers();
    file.result = { data: { content: "x", encoding: "utf-8" } };
    const { rerender } = render(<GoToFilePreview sessionId="conv" path="a.ts" />);
    expect(file.read).toEqual(["a.ts"]);
    rerender(<GoToFilePreview sessionId="conv" path="b.ts" />);
    act(() => vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS / 2));
    rerender(<GoToFilePreview sessionId="conv" path="c.ts" />);
    // The previous file stays in view meanwhile.
    expect(screen.getByTestId("go-to-file-preview")).toHaveTextContent("a.ts");
    act(() => vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS));
    expect(file.read).toEqual(["a.ts", "c.ts"]);
    expect(screen.getByTestId("go-to-file-preview")).toHaveTextContent("c.ts");
  });
});
