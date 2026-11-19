import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type FileContentResponse, useFileContent } from "@/hooks/useFileContent";
import type { CodeMirrorFileEditor } from "@/shell/codeMirror/CodeMirrorFileEditor";
import type { FileViewer } from "@/shell/FileViewer";

import { type FilesPageLeaveGuard, FilesPageViewer, showsInFileViewer } from "./FilesPageViewer";

const captured = vi.hoisted(() => ({
  editor: null as ComponentProps<typeof CodeMirrorFileEditor> | null,
  viewer: null as ComponentProps<typeof FileViewer> | null,
}));

vi.mock("@/hooks/useFileContent", () => ({ useFileContent: vi.fn() }));
vi.mock("@/shell/codeMirror/CodeMirrorFileEditor", () => ({
  CodeMirrorFileEditor: (props: ComponentProps<typeof CodeMirrorFileEditor>) => {
    captured.editor = props;
    return <div data-testid="editor">{props.content}</div>;
  },
}));
vi.mock("@/shell/FileViewer", () => ({
  FileViewer: (props: ComponentProps<typeof FileViewer>) => {
    captured.viewer = props;
    return <div data-testid="file-viewer">{props.path}</div>;
  },
}));

type FileResult = ReturnType<typeof useFileContent>;

function file(path: string, content: string, extra: Partial<FileContentResponse> = {}) {
  return {
    object: "session.environment.filesystem.file_content",
    path,
    content_type: null,
    encoding: "utf-8",
    content,
    bytes: content.length,
    ...extra,
  } satisfies FileContentResponse;
}

function renderViewer(
  path: string,
  result: Partial<FileResult>,
  props: Partial<ComponentProps<typeof FilesPageViewer>> = {},
) {
  vi.mocked(useFileContent).mockReturnValue({ isFetching: false, ...result } as FileResult);
  const onClose = vi.fn();
  const view = render(
    <FilesPageViewer conversationId="conv_1" path={path} active onClose={onClose} {...props} />,
  );
  return { onClose, ...view };
}

beforeEach(() => {
  captured.editor = null;
  captured.viewer = null;
  vi.mocked(useFileContent).mockReset();
});
afterEach(cleanup);

describe("showsInFileViewer", () => {
  it("sends Markdown, images, PDFs, 3D models and binary files to the app's viewer, and other text to the editor", () => {
    expect(showsInFileViewer("logo.png", null)).toBe(true);
    expect(showsInFileViewer("guide.pdf", null)).toBe(true);
    expect(showsInFileViewer("part.stl", null)).toBe(true);
    expect(showsInFileViewer("README.md", null)).toBe(true);
    expect(showsInFileViewer("src/a.ts", null)).toBe(false);
  });
});

describe("FilesPageViewer", () => {
  it("reads the file only while its tab is on screen", () => {
    renderViewer("a.ts", { data: file("a.ts", "x") }, { active: false });
    expect(useFileContent).toHaveBeenCalledWith("conv_1", "a.ts", { visible: false });
  });

  it("edits text in CodeMirror at the cited position, with Changes and Close in its toolbar", async () => {
    const onShowChanges = vi.fn();
    const position = { line: 3 };
    const { onClose } = renderViewer(
      "src/a.ts",
      { data: file("src/a.ts", "const a = 1;", { truncated: true }) },
      { onShowChanges, position },
    );
    expect(screen.getByTestId("editor")).toHaveTextContent("const a = 1;");
    expect(captured.editor).toMatchObject({ path: "src/a.ts", truncated: true, position });
    await userEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(onShowChanges).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Close file" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("offers no Changes button where the file has no changes view", () => {
    renderViewer("a.ts", { data: file("a.ts", "x") });
    expect(screen.queryByRole("button", { name: "Changes" })).toBeNull();
  });

  it("shows an image in the app's viewer, and text-named bytes that aren't text there too", () => {
    renderViewer("logo.png", { data: file("logo.png", "", { content_type: "image/png" }) });
    expect(screen.getByTestId("file-viewer")).toHaveTextContent("logo.png");
    expect(screen.queryByTestId("editor")).toBeNull();
    cleanup();
    renderViewer("data.txt", {
      data: file("data.txt", btoa("\xff\xfe\xfd"), { encoding: "base64" }),
    });
    expect(screen.getByTestId("file-viewer")).toHaveTextContent("data.txt");
    expect(screen.queryByTestId("editor")).toBeNull();
  });

  it("opens Markdown in the app's rich preview, which asks the tab's leave question for its edits", () => {
    const registerLeaveGuard = vi.fn(() => () => {});
    const position = { line: 4 };
    const { onClose } = renderViewer(
      "docs/guide.md",
      { data: file("docs/guide.md", "# Guide") },
      { registerLeaveGuard, position },
    );
    expect(screen.getByTestId("file-viewer")).toHaveTextContent("docs/guide.md");
    expect(screen.queryByTestId("editor")).toBeNull();
    expect(captured.viewer).toMatchObject({
      conversationId: "conv_1",
      path: "docs/guide.md",
      position,
      open: true,
      frameless: true,
      registerLeaveGuard,
    });
    captured.viewer!.onClose();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("gives the app's viewer the session's permission level, for rich files and bytes that aren't text", () => {
    renderViewer(
      "docs/guide.md",
      { data: file("docs/guide.md", "# Guide") },
      { permissionLevel: 1 },
    );
    expect(captured.viewer!.permissionLevel).toBe(1);
    cleanup();
    captured.viewer = null;
    renderViewer(
      "data.txt",
      { data: file("data.txt", btoa("\xff\xfe\xfd"), { encoding: "base64" }) },
      { permissionLevel: 1 },
    );
    expect(captured.viewer!.permissionLevel).toBe(1);
  });

  it("keeps the tree toggle, Changes and Close above a rich file, without repeating its path", async () => {
    const onShowChanges = vi.fn();
    const { onClose } = renderViewer(
      "docs/guide.md",
      { data: file("docs/guide.md", "# Guide") },
      { toolbarLeading: <button type="button">Show tree</button>, onShowChanges },
    );
    expect(screen.getByRole("button", { name: "Show tree" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(onShowChanges).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Close file" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    // The viewer's own header names the file.
    expect(screen.getAllByText("docs/guide.md")).toHaveLength(1);
  });

  it("shows a spinner while loading, and why a file couldn't open", () => {
    renderViewer("a.ts", { data: undefined });
    expect(screen.getByLabelText("Loading a.ts")).toBeInTheDocument();
    cleanup();
    renderViewer("a.ts", { isError: true, error: new Error("not found") } as never);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t open a.ts: not found");
  });

  describe("unsaved edits", () => {
    function renderDirty() {
      const unregister = vi.fn();
      let guard: FilesPageLeaveGuard | undefined;
      const registerLeaveGuard = vi.fn((next: FilesPageLeaveGuard) => {
        guard = next;
        return unregister;
      });
      const view = renderViewer("a.ts", { data: file("a.ts", "x") }, { registerLeaveGuard });
      return { registerLeaveGuard, unregister, guard: () => guard!, ...view };
    }

    it("asks before leaving only while the file has unsaved edits", () => {
      const { registerLeaveGuard, unregister } = renderDirty();
      expect(registerLeaveGuard).not.toHaveBeenCalled();
      act(() => captured.editor!.onDirtyChange!(true));
      expect(registerLeaveGuard).toHaveBeenCalledTimes(1);
      act(() => captured.editor!.onDirtyChange!(false));
      expect(unregister).toHaveBeenCalledTimes(1);
    });

    it("leaves on Discard changes and stays on Keep editing", async () => {
      const { guard } = renderDirty();
      act(() => captured.editor!.onDirtyChange!(true));
      let answer: Promise<string>;
      act(() => {
        answer = guard()();
      });
      await userEvent.click(await screen.findByRole("button", { name: "Discard changes" }));
      await expect(answer!).resolves.toBe("leave");
      act(() => {
        answer = guard()();
      });
      await userEvent.click(await screen.findByRole("button", { name: "Keep editing" }));
      await expect(answer!).resolves.toBe("cancel");
    });

    it("keeps the edits when the file closes while the question is open", async () => {
      const { guard, unmount } = renderDirty();
      act(() => captured.editor!.onDirtyChange!(true));
      let answer: Promise<string>;
      act(() => {
        answer = guard()();
      });
      await screen.findByRole("button", { name: "Discard changes" });
      unmount();
      await expect(answer!).resolves.toBe("cancel");
    });
  });
});
