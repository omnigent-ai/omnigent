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

function renderedResponse(text: string, leadingCommand?: string) {
  const { container, unmount } = render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <FilePathAwareMessageResponse breaks mode="static" leadingCommand={leadingCommand}>
          {text}
        </FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  const rendered = { html: container.innerHTML, text: container.textContent };
  unmount();
  return rendered;
}

describe("FilePathAwareMessageResponse leadingCommand", () => {
  // The tint wraps the token without changing how the rest of the text parses.
  // `shown` is text that must stay on screen with the tint.
  it.each([
    // Alone, these args would parse as a link definition and render nothing.
    { args: " [docs]: https://example.com", shown: "/team [docs]: https://example.com" },
    { args: " # x", shown: "/team" },
    { args: " - x", shown: "/team" },
    { args: " - first\n- second", shown: "/team" },
    { args: " ---", shown: "/team" },
    { args: " $$\nx^2\n$$", shown: "/team" },
    { args: "\n    const x = 1", shown: "/team" },
  ])("renders /team$args like the untinted text", ({ args, shown }) => {
    const text = `/team${args}`;
    const untinted = renderedResponse(text);
    const tinted = renderedResponse(text, "/team");
    expect(tinted.html).toContain(TINTED_TEAM);
    expect(tinted.html.replace(TINTED_TEAM, "/team")).toBe(untinted.html);
    expect(tinted.text).toContain(shown);
  });
});
