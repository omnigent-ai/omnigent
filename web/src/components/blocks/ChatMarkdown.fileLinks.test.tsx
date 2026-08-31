// End-to-end rendering of a markdown link whose href is a workspace file.
//
// Agents link files they just wrote (`[foo.md](/abs/ws/foo.md)`). Two ways this
// used to go wrong, both reproduced here so a regression is loud:
//
//   - An absolute path stayed a real anchor, so clicking it navigated the app
//     origin and the server answered {"detail":"Not Found"}.
//   - A path with no leading slash had its href stripped and " [blocked]"
//     appended, which read as though the app had censored the link.
//
// Both should instead open the FileViewer, exactly as an inline-code path does.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type * as NativeBridge from "@/lib/nativeBridge";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const nativeShell = vi.hoisted(() => ({ electron: false }));

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isElectronShell: () => nativeShell.electron,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  nativeShell.electron = false;
});

const WORKSPACE = "/home/u/ws";

// Module scope so the provider value is a constant (jsx-no-constructed-context-values);
// `renderMarkdown` swaps the changed-file list and resets the spy per test.
const openFile = vi.fn();
let changedPaths: string[] = [];
const FILE_VIEWER = {
  openFile,
  isChangedPath: (p: string) => changedPaths.includes(p),
  conversationId: undefined,
  workspaceRoot: WORKSPACE,
  workspaceHome: "/home/u",
};

function renderMarkdown(markdown: string, changed: string[] = []): void {
  changedPaths = changed;
  openFile.mockClear();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
}

describe("markdown links to workspace files", () => {
  it("opens the FileViewer for an absolute path instead of navigating", () => {
    renderMarkdown(`[proposal.md](${WORKSPACE}/docs/proposal.md)`, ["docs/proposal.md"]);

    const link = screen.getByRole("button", { name: "proposal.md" });
    // No href at all: the parked fragment must not survive as clickable.
    expect(link).not.toHaveAttribute("href");
    expect(link).not.toHaveAttribute("target");
    expect(link).not.toHaveAttribute("rel");

    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("docs/proposal.md");
  });

  it("opens the FileViewer for a relative path instead of showing it as blocked", () => {
    renderMarkdown("[notes.md](docs/notes.md)", ["docs/notes.md"]);

    expect(screen.queryByText(/\[blocked\]/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens on keyboard activation", () => {
    renderMarkdown("[notes.md](docs/notes.md)", ["docs/notes.md"]);

    fireEvent.keyDown(screen.getByRole("button", { name: "notes.md" }), { key: "Enter" });
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("leaves an external web link as a real current-tab anchor", () => {
    renderMarkdown("[docs](https://example.com/page)");

    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/page");
    expect(link).toHaveAttribute("target", "_self");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("keeps Electron external links on the desktop popup-policy path", () => {
    nativeShell.electron = true;
    renderMarkdown("[docs](https://example.com/page)");

    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/page");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("leaves primary-click current-tab navigation to the browser", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    renderMarkdown("[docs](https://example.com/page)");

    const link = screen.getByRole("link", { name: "docs" });
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    link.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
  });

  it.each([
    ["ctrl-click", "click", { button: 0, ctrlKey: true }],
    ["meta-click", "click", { button: 0, metaKey: true }],
    ["shift-click", "click", { button: 0, shiftKey: true }],
    ["middle-click", "auxclick", { button: 1 }],
  ])("leaves %s to the browser", (_label, type, init) => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    renderMarkdown("[docs](https://example.com/page)");

    const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init });
    screen.getByRole("link", { name: "docs" }).dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("keeps dangerous link targets blocked", () => {
    renderMarkdown("[unsafe](javascript:alert(1))");

    expect(screen.queryByRole("link", { name: "unsafe" })).toBeNull();
  });

  // Overriding the `a` slot replaces Streamdown's link component, so its
  // styling and marker attribute have to be reproduced. index.css keys the
  // pointer cursor and the table-cell overflow-wrap rule (which stops a
  // link-only table column collapsing to ~2ch) on the attribute.
  it.each([
    ["an external link", "[docs](https://example.com/page)", "docs", [] as string[]],
    ["a workspace file link", "[notes.md](docs/notes.md)", "notes.md", ["docs/notes.md"]],
  ])("keeps Streamdown's link styling on %s", (_label, markdown, name, changed) => {
    renderMarkdown(markdown, changed);

    const link = screen.getByText(name).closest("a");
    expect(link).toHaveAttribute("data-streamdown", "link");
    expect(link).toHaveClass("wrap-anywhere", "font-medium", "text-primary", "underline");
  });

  it("keeps the source hast node out of the DOM", () => {
    // Streamdown passes `node` to every override; spreading it onto the element
    // renders a literal node="[object Object]" attribute.
    renderMarkdown("[docs](https://example.com/page) and `docs/notes.md`", ["docs/notes.md"]);

    expect(screen.getByRole("link", { name: "docs" })).not.toHaveAttribute("node");
    expect(screen.getByRole("button", { name: "docs/notes.md" })).not.toHaveAttribute("node");
  });

  it("renders a path that names no workspace file as plain text, not a dead link", () => {
    // Not in the changed list and no conversation id, so no existence check can
    // confirm it, so it must not become an anchor on the parked fragment.
    renderMarkdown("[ghost.md](docs/ghost.md)");

    expect(screen.queryByRole("link", { name: "ghost.md" })).toBeNull();
    expect(screen.queryByRole("button", { name: "ghost.md" })).toBeNull();
    expect(screen.getByText("ghost.md")).toBeInTheDocument();
    expect(screen.queryByText(/\[blocked\]/)).toBeNull();
  });

  it("still linkifies an inline-code path", () => {
    renderMarkdown("see `docs/notes.md` for detail", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });
});

// `path:line` is how agents habitually cite a file. The position is not part of
// the filename, so resolving the span verbatim matched nothing on disk and the
// citation rendered as inert inline code.
describe("cited positions", () => {
  it("opens an inline-code path that carries a line number", () => {
    renderMarkdown("`docs/notes.md:12` has the detail", ["docs/notes.md"]);

    // The span still shows what the agent wrote; only the target drops :12.
    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md:12" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens an absolute inline-code path with a line number", () => {
    // The shape from the report: an absolute path under the workspace root.
    renderMarkdown(`\`${WORKSPACE}/docs/notes.md:1\` contains hi`, ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: `${WORKSPACE}/docs/notes.md:1` }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens a path citing both line and column", () => {
    renderMarkdown("`docs/notes.md:12:7` is the spot", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "docs/notes.md:12:7" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("opens a markdown link whose href carries a line number", () => {
    renderMarkdown("[notes.md](docs/notes.md:12)", ["docs/notes.md"]);

    fireEvent.click(screen.getByRole("button", { name: "notes.md" }));
    expect(openFile).toHaveBeenCalledWith("docs/notes.md");
  });

  it("leaves a trailing-colon span that is not a position alone", () => {
    // "Note:" style prose must not be mistaken for a path with a position.
    renderMarkdown("`docs/notes.md:abc` is not a position", ["docs/notes.md"]);

    expect(screen.queryByRole("button", { name: "docs/notes.md:abc" })).toBeNull();
  });
});
