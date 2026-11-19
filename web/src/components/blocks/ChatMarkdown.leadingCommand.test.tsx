import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};
const TINTED_TEAM = `<span class="${COMMAND_TOKEN_CLASS}">/team</span>`;

beforeEach(() => {
  // File links may probe the workspace; keep that pending instead of hitting the network.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderedHtml(text: string, leadingCommand?: string) {
  const { container, unmount } = render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <FilePathAwareMessageResponse breaks mode="static" leadingCommand={leadingCommand}>
          {text}
        </FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  const html = container.innerHTML;
  unmount();
  return html;
}

describe("FilePathAwareMessageResponse leadingCommand", () => {
  // The tint wraps the token without changing how the rest of the text parses.
  it.each([
    // Alone, these args would parse as a link definition and render nothing.
    " [docs]: https://example.com",
    " # x",
    " - x",
    " - first\n- second",
    " ---",
    " $$\nx^2\n$$",
    "\n    const x = 1",
  ])("renders /team%j like the untinted text", (args) => {
    const text = `/team${args}`;
    const untinted = renderedHtml(text);
    const tinted = renderedHtml(text, "/team");
    expect(tinted).toContain(TINTED_TEAM);
    expect(tinted.replace(TINTED_TEAM, "/team")).toBe(untinted);
  });

  it("keeps the args of a link-definition-shaped command visible", () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <FilePathAwareMessageResponse breaks mode="static" leadingCommand="/team">
            {"/team [docs]: https://example.com"}
          </FilePathAwareMessageResponse>
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    expect(container).toHaveTextContent("/team [docs]: https://example.com");
  });
});
