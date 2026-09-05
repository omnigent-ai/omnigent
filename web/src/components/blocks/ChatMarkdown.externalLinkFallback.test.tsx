// A chat link rendered with target="_blank" is unusable where the embedding
// host withholds popup creation (e.g. a workspace browser pane): the click is
// silently swallowed — no new tab, no navigation. The link renderer must keep
// the new-tab behavior where popups work and fall back to a same-tab
// navigation where they don't, while leaving modified clicks (and native
// shells, whose window-open policy routes links itself) to the platform.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type * as NativeBridge from "@/lib/nativeBridge";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const nativeShell = vi.hoisted(() => ({ native: false }));

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => nativeShell.native,
}));

const LINK_URL = "https://example.com/page";

let assignedUrls: string[];
let originalLocation: Location;

beforeEach(() => {
  assignedUrls = [];
  originalLocation = window.location;
  // Capture same-tab navigations without jsdom trying to perform them.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      ...originalLocation,
      assign: (url: string) => {
        assignedUrls.push(url);
      },
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  nativeShell.native = false;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

const FILE_VIEWER = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: "/home/u/ws",
  workspaceHome: "/home/u",
};

function renderExternalLink(): HTMLElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <FilePathAwareMessageResponse>{`[docs](${LINK_URL})`}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return screen.getByRole("link", { name: "docs" });
}

function click(link: HTMLElement, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init });
  link.dispatchEvent(event);
  return event;
}

describe("external chat link clicks", () => {
  it("keeps the new-tab attributes Streamdown renders", () => {
    const link = renderExternalLink();
    expect(link).toHaveAttribute("href", LINK_URL);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("opens a new tab on a plain click where popups are granted", () => {
    const popup = { opener: window } as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(true);
    expect(openSpy).toHaveBeenCalledWith(LINK_URL, "_blank");
    // The rel="noreferrer" a native _blank click applies must not be lost.
    expect(popup.opener).toBeNull();
    expect(assignedUrls).toEqual([]);
  });

  it("falls back to a same-tab navigation where popup creation is withheld", () => {
    vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(true);
    expect(assignedUrls).toEqual([LINK_URL]);
  });

  it.each([
    ["ctrl-click", { ctrlKey: true }],
    ["meta-click", { metaKey: true }],
    ["shift-click", { shiftKey: true }],
    ["alt-click", { altKey: true }],
    ["middle-click", { button: 1 }],
  ])("leaves %s to the browser", (_label, init) => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink(), init);

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
    expect(assignedUrls).toEqual([]);
  });

  it("leaves native shells on their own window-open policy", () => {
    // A native shell routes _blank externally and reports null from
    // window.open regardless, so the fallback would navigate twice.
    nativeShell.native = true;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
    expect(assignedUrls).toEqual([]);
  });
});
