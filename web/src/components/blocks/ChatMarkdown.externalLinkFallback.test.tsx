// A chat link rendered with target="_blank" is unusable where the embedding
// host withholds popup creation (e.g. a workspace browser pane): the click is
// silently swallowed — no new tab, no navigation. The link renderer must keep
// the new-tab behavior where popups work and fall back to a same-tab
// navigation where they don't, while leaving modified clicks (and native
// shells, whose window-open policy routes links itself) to the platform.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as NativeBridge from "@/lib/nativeBridge";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const nativeShell = vi.hoisted(() => ({ native: false }));

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => nativeShell.native,
}));

const LINK_URL = "https://example.com/page";

let followedLinks: HTMLAnchorElement[];

beforeEach(() => {
  followedLinks = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    followedLinks.push(this);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  nativeShell.native = false;
});

const FILE_VIEWER = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: "/home/u/ws",
  workspaceHome: "/home/u",
};

function renderExternalLink(href = LINK_URL): HTMLElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <FilePathAwareMessageResponse>{`[docs](${href})`}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return screen.getByText(/^docs(?: \[blocked\])?$/);
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
    const popupDocument = document.implementation.createHTMLDocument();
    const popup = { opener: window, document: popupDocument } as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(true);
    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(followedLinks).toHaveLength(1);
    expect(followedLinks[0].ownerDocument).toBe(popupDocument);
    expect(followedLinks[0].href).toBe(LINK_URL);
    expect(followedLinks[0].target).toBe("_self");
    expect(followedLinks[0].rel).toBe("noopener noreferrer");
  });

  it("falls back to a same-tab navigation where popup creation is withheld", () => {
    vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(true);
    expect(followedLinks).toHaveLength(1);
    expect(followedLinks[0].ownerDocument).toBe(document);
    expect(followedLinks[0].href).toBe(LINK_URL);
    expect(followedLinks[0].target).toBe("_self");
    expect(followedLinks[0].rel).toBe("noopener noreferrer");
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
    expect(followedLinks).toEqual([]);
  });

  it("leaves native shells on their own window-open policy", () => {
    // A native shell routes _blank externally and reports null from
    // window.open regardless, so the fallback would navigate twice.
    nativeShell.native = true;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
    expect(followedLinks).toEqual([]);
  });

  it("does not follow an already-cancelled click", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const link = renderExternalLink();
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    event.preventDefault();
    link.dispatchEvent(event);

    expect(openSpy).not.toHaveBeenCalled();
    expect(followedLinks).toEqual([]);
  });

  it("leaves mail links to the browser without opening an empty tab", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    const event = click(renderExternalLink("mailto:docs@example.com"));

    expect(event.defaultPrevented).toBe(false);
    expect(openSpy).not.toHaveBeenCalled();
    expect(followedLinks).toEqual([]);
  });

  it.each(["javascript:invalid", "data:text/plain,invalid", "vbscript:invalid"])(
    "never follows a sanitized %s URL",
    (href) => {
      const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

      click(renderExternalLink(href));

      expect(screen.queryByRole("link")).not.toBeInTheDocument();
      expect(openSpy).not.toHaveBeenCalled();
      expect(followedLinks).toEqual([]);
    },
  );
});
