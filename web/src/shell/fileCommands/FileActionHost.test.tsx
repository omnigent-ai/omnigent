import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as EditorLinksModule from "@/lib/editorLinks";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import type * as RevealInFileManagerModule from "../RevealInFileManager";
import { isCommandListed, usePaletteCommandsStore } from "@/store/paletteCommands";
import { FileActionHost, fileLink } from "./FileActionHost";
import { requestFileAction, useFileActionStore } from "./fileActionStore";
import { publishFocusedFile, useFocusedFileStore } from "./focusedFile";
import { usePreferredEditor } from "./preferredEditor";
import { FileViewerContext } from "../FileViewerContext";
import { resolveFileCommandTarget } from "./target";
import { useFileCommandMenu } from "./useFileCommandMenu";

const { copyText, launchEditorLink, revealInFileManager, toast, shell } = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
  launchEditorLink: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
  revealInFileManager: vi.fn(),
  shell: { ios: false, reveal: true },
}));
vi.mock("@/lib/clipboard", () => ({ copyText }));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/lib/editorLinks", async (importOriginal) => ({
  ...(await importOriginal<typeof EditorLinksModule>()),
  launchEditorLink,
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isIOSShell: () => shell.ios,
  supportsFileReveal: () => shell.reveal,
}));
vi.mock("@/lib/desktopHostId", () => ({ useDesktopHostId: () => "this-mac" }));
vi.mock("../RevealInFileManager", async (importOriginal) => ({
  ...(await importOriginal<typeof RevealInFileManagerModule>()),
  fileManagerName: () => "Finder",
  revealInFileManager,
}));
vi.mock("@/hooks/useHostSshTarget", () => ({
  useHostSshTarget: (hostId: string | null) => (hostId === "dev-box" ? "dev-box.ssh" : null),
}));

function target(
  path: string,
  overrides: Parameters<typeof resolveFileCommandTarget>[1] = {
    root: "/repo",
    hostId: "this-mac",
    conversationId: "conv_1",
  },
) {
  return resolveFileCommandTarget(path, overrides);
}

beforeEach(() => {
  copyText.mockReset().mockResolvedValue(undefined);
  shell.ios = false;
  shell.reveal = true;
  render(<FileActionHost />);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useFileActionStore.setState({ pending: [] });
  useFocusedFileStore.setState({ target: null, element: null });
});

describe("FileActionHost", () => {
  it("copies the full path, and says when only the workspace path was known", async () => {
    act(() => requestFileAction(target("src/main.ts"), "copy-path"));
    expect(copyText).toHaveBeenLastCalledWith("/repo/src/main.ts");
    await waitFor(() => expect(toast.success).toHaveBeenLastCalledWith("Copied path"));

    act(() =>
      requestFileAction(
        target("src/main.ts", { root: null, hostId: "this-mac", conversationId: "conv_1" }),
        "copy-path",
      ),
    );
    expect(copyText).toHaveBeenLastCalledWith("src/main.ts");
    await waitFor(() => expect(toast.success).toHaveBeenLastCalledWith("Copied relative path"));
    // A pull request file's repository path is its whole path.
    act(() =>
      requestFileAction(
        resolveFileCommandTarget("src/app.ts", {
          root: null,
          hostId: null,
          conversationId: "conv_1",
          repositoryFile: true,
        }),
        "copy-path",
      ),
    );
    expect(copyText).toHaveBeenLastCalledWith("src/app.ts");
    await waitFor(() => expect(toast.success).toHaveBeenLastCalledWith("Copied path"));
  });

  it("reports a failed copy without claiming success", async () => {
    copyText.mockRejectedValue(new Error("denied"));
    act(() => requestFileAction(target("src/main.ts"), "copy-relative-path"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't copy the relative path"),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("links into the target's own session", () => {
    act(() =>
      requestFileAction(
        target("docs/a b.md", { root: "/repo", hostId: "this-mac", conversationId: "conv_2" }),
        "copy-link",
      ),
    );
    const link = new URL(copyText.mock.calls[0]![0]);
    expect(link.pathname).toBe("/c/conv_2");
    expect(link.searchParams.get("file")).toBe("docs/a b.md");
  });

  it("runs each session's action once, with that session's path and link", () => {
    const first = target("src/a.ts", {
      root: "/repo-a",
      hostId: "this-mac",
      conversationId: "conv_a",
    });
    const second = target("lib/b.ts", {
      root: "/repo-b",
      hostId: "this-mac",
      conversationId: "conv_b",
    });
    act(() => requestFileAction(first, "copy-link"));
    act(() => requestFileAction(second, "copy-link"));
    act(() => requestFileAction(first, "copy-path"));
    act(() => requestFileAction(second, "copy-path"));

    const copied = copyText.mock.calls.map(([text]) => text);
    expect(copied).toHaveLength(4);
    const links = copied.slice(0, 2).map((text) => new URL(text));
    expect(links.map((link) => [link.pathname, link.searchParams.get("file")])).toEqual([
      ["/c/conv_a", "src/a.ts"],
      ["/c/conv_b", "lib/b.ts"],
    ]);
    expect(copied.slice(2)).toEqual(["/repo-a/src/a.ts", "/repo-b/lib/b.ts"]);
  });

  it("runs two requests made at once, in order", () => {
    act(() => {
      requestFileAction(target("src/a.ts"), "copy-path");
      requestFileAction(target("src/b.ts"), "copy-relative-path");
    });
    expect(copyText.mock.calls.map(([text]) => text)).toEqual(["/repo/src/a.ts", "src/b.ts"]);
    expect(useFileActionStore.getState().pending).toEqual([]);
  });

  it("shows this machine's files in the file manager", () => {
    act(() => requestFileAction(target("src/main.ts"), "reveal"));
    expect(revealInFileManager).toHaveBeenCalledWith({
      hostId: "this-mac",
      path: "/repo/src/main.ts",
    });
  });

  it("reveals nothing in a desktop shell too old to show files", () => {
    shell.reveal = false;
    act(() => requestFileAction(target("src/main.ts"), "reveal"));
    expect(revealInFileManager).not.toHaveBeenCalled();
  });

  it("opens this machine's files in an editor directly, and remembers the editor", () => {
    // The Open in menu reads the same preference, so its default follows this choice.
    const preferred = renderHook(() => usePreferredEditor());
    act(() => requestFileAction(target("src/main.ts"), "open-in-vscode"));
    expect(launchEditorLink).toHaveBeenLastCalledWith("vscode://file/repo/src/main.ts");
    expect(preferred.result.current).toBe("vscode");
    act(() => requestFileAction(target("src/main.ts"), "open-in-cursor"));
    expect(launchEditorLink).toHaveBeenLastCalledWith("cursor://file/repo/src/main.ts");
    expect(preferred.result.current).toBe("cursor");
  });

  it("opens no editor from a phone, where neither local nor SSH links work", () => {
    shell.ios = true;
    act(() => requestFileAction(target("src/main.ts"), "open-in-vscode"));
    act(() =>
      requestFileAction(
        target("src/main.ts", { root: "/repo", hostId: "dev-box", conversationId: "conv_1" }),
        "open-in-cursor",
      ),
    );
    expect(launchEditorLink).not.toHaveBeenCalled();
  });

  it("runs each request once under StrictMode, which runs effects twice", () => {
    cleanup();
    render(
      <StrictMode>
        <FileActionHost />
      </StrictMode>,
    );
    act(() => requestFileAction(target("src/main.ts"), "open-in-vscode"));
    act(() => requestFileAction(target("src/main.ts"), "copy-path"));
    expect(launchEditorLink).toHaveBeenCalledTimes(1);
    expect(copyText).toHaveBeenCalledTimes(1);
  });

  it("opens a remote host's file over SSH, but never reveals it", () => {
    const remote = target("src/main.ts", {
      root: "/repo",
      hostId: "dev-box",
      conversationId: "conv_1",
    });
    act(() => requestFileAction(remote, "reveal"));
    expect(revealInFileManager).not.toHaveBeenCalled();
    act(() => requestFileAction(remote, "open-in-cursor"));
    expect(launchEditorLink).toHaveBeenCalledWith(expect.stringContaining("dev-box.ssh"));
  });

  it("does nothing for an action that no longer applies", () => {
    const deleted = resolveFileCommandTarget("gone.ts", {
      root: "/repo",
      hostId: "this-mac",
      conversationId: "conv_1",
      deleted: true,
    });
    act(() => requestFileAction(deleted, "reveal"));
    act(() => requestFileAction(deleted, "open-in-vscode"));
    act(() =>
      requestFileAction(target("a.md", { root: "/repo", hostId: "this-mac" }), "copy-link"),
    );
    expect(revealInFileManager).not.toHaveBeenCalled();
    expect(launchEditorLink).not.toHaveBeenCalled();
    expect(copyText).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(useFileActionStore.getState().pending).toEqual([]);
  });

  it("builds links under the deployment's base path, keeping the surface's link state", () => {
    expect(fileLink("conv_1", "a.md", (path) => `/base${path}`, "", "https://example.com")).toBe(
      "https://example.com/base/c/conv_1?file=a.md",
    );
    expect(
      fileLink("conv_1", "a.md", (path) => path, "file=old.md&diff=1", "https://example.com"),
    ).toBe("https://example.com/c/conv_1?file=a.md&diff=1");
  });
});

describe("the focused file in ⌘K", () => {
  function fileGroup() {
    return Object.entries(usePaletteCommandsStore.getState().groups).find(([source]) =>
      source.startsWith("file:focused:"),
    )?.[1];
  }

  it("offers the focused row's actions until focus moves elsewhere", () => {
    const row = document.createElement("button");
    const elsewhere = document.createElement("button");
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    const menuItem = document.createElement("button");
    menu.append(menuItem);
    document.body.append(row, elsewhere, menu);

    act(() => publishFocusedFile(row, target("src/main.ts")));
    const group = fileGroup();
    expect(group?.heading).toBe("File");
    // The row opens itself, so ⌘K doesn't repeat Open.
    const listed = group!.commands.filter((command) => isCommandListed(command.availability));
    expect(listed.map((command) => command.id)).toEqual([
      "file.copy-path",
      "file.copy-relative-path",
      "file.copy-link",
      "file.reveal",
      "file.open-in-vscode",
      "file.open-in-cursor",
    ]);

    act(() => group!.commands.find((command) => command.id === "file.copy-path")!.run());
    expect(copyText).toHaveBeenCalledWith("/repo/src/main.ts");

    // The palette and menus act on the row focus came from.
    act(() => menuItem.focus());
    expect(fileGroup()).toBeDefined();
    act(() => elsewhere.focus());
    expect(fileGroup()).toBeUndefined();
    row.remove();
    elsewhere.remove();
    menu.remove();
  });

  function viewer(workspaceRoot: string | null) {
    return {
      openFile: vi.fn(),
      registerNavigationGuard: () => () => {},
      openGithubTab: vi.fn(),
      isChangedPath: () => false,
      conversationId: "conv_1",
      workspaceRoot,
      workspaceHome: null,
      sessionHostId: "this-mac",
    };
  }

  function Row({ path }: { path: string }) {
    const fileMenu = useFileCommandMenu(path);
    return (
      <div {...fileMenu.rowProps}>
        <button type="button">{path}</button>
        {fileMenu.menu}
      </div>
    );
  }

  function renderRow(workspaceRoot: string | null) {
    const context = viewer(workspaceRoot);
    return (
      <FileViewerContext.Provider value={context}>
        <Row path="src/main.ts" />
      </FileViewerContext.Provider>
    );
  }

  function runPaletteCommand(id: string) {
    act(() =>
      fileGroup()!
        .commands.find((command) => command.id === id)!
        .run(),
    );
  }

  it("acts on the focused row's current facts once the workspace location loads", () => {
    const view = render(renderRow(null));
    act(() => screen.getByRole("button", { name: "src/main.ts" }).focus());
    view.rerender(renderRow("/repo"));
    runPaletteCommand("file.copy-path");
    expect(copyText).toHaveBeenCalledWith("/repo/src/main.ts");
  });

  it("withdraws a focused row's actions when the row unmounts", () => {
    const view = render(renderRow("/repo"));
    act(() => screen.getByRole("button", { name: "src/main.ts" }).focus());
    expect(fileGroup()).toBeDefined();
    view.unmount();
    expect(fileGroup()).toBeUndefined();
  });
});
