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
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

afterEach(cleanup);

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

  it("leaves an external link as a real anchor", () => {
    renderMarkdown("[docs](https://example.com/page)");

    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/page");
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
