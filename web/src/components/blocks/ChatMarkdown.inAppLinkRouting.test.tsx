// On the desktop shell, a plain click on an external chat link normally rides
// the shell's window-open policy out to the default OS browser. When the user
// opts in (the "Open links in the in-app browser" setting), that click must
// instead route the URL into the conversation's embedded browser view and
// surface the Browser pane — while modified clicks, non-web schemes, and
// contexts with no conversation keep the default external path, the
// preference stays off by default, and a view that refuses the link falls
// back to the external browser instead of swallowing the click.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import type * as NativeBridge from "@/lib/nativeBridge";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => true,
}));

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const LINK_URL = "https://example.com/page";
const CONVERSATION_ID = "conv-embedded-browser";

let openOrNavigate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  openOrNavigate = vi.fn().mockResolvedValue({ ok: true });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: () => {},
    notify: () => Promise.resolve(false),
    browserOpenOrNavigate: openOrNavigate,
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.mocked(showToast).mockReset();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

const FILE_VIEWER: NonNullable<React.ContextType<typeof FileViewerContext>> = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: CONVERSATION_ID,
  workspaceRoot: "/home/u/ws",
  workspaceHome: "/home/u",
};

// A context with no conversation — e.g. markdown rendered outside a chat.
const FILE_VIEWER_NO_CONVERSATION: NonNullable<React.ContextType<typeof FileViewerContext>> = {
  ...FILE_VIEWER,
  conversationId: undefined,
};

function renderExternalLink(href = LINK_URL, fileViewer = FILE_VIEWER): HTMLElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={fileViewer}>
        <FilePathAwareMessageResponse>{`[docs](${href})`}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  return screen.getByText("docs");
}

function click(link: HTMLElement, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init });
  link.dispatchEvent(event);
  return event;
}

describe("desktop chat link clicks with the in-app browser preference on", () => {
  beforeEach(() => {
    writeOpenLinksInApp(true);
  });

  it("routes a plain click into the conversation's embedded browser view", async () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const surfaced: string[] = [];
    const unsubscribe = onInAppLinkOpen((conversationId) => surfaced.push(conversationId));

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(true);
    expect(openOrNavigate).toHaveBeenCalledWith(CONVERSATION_ID, LINK_URL);
    // The Browser tab surfaces once the view accepted the URL.
    await vi.waitFor(() => expect(surfaced).toEqual([CONVERSATION_ID]));
    unsubscribe();
    expect(openSpy).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("falls back to the external browser when the embedded view refuses the link", async () => {
    openOrNavigate.mockResolvedValue({ ok: false, error: "browser view cap reached — close one" });
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const surfaced: string[] = [];
    const unsubscribe = onInAppLinkOpen((conversationId) => surfaced.push(conversationId));

    const event = click(renderExternalLink());

    // The anchor's own _blank navigation was cancelled, so the fallback is
    // the only way the click still reaches a browser.
    expect(event.defaultPrevented).toBe(true);
    await vi.waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(LINK_URL, "_blank", "noopener,noreferrer"),
    );
    unsubscribe();
    expect(surfaced).toEqual([]);
    expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain(
      "browser view cap reached — close one",
    );
  });

  it.each([
    ["ctrl-click", { ctrlKey: true }],
    ["meta-click", { metaKey: true }],
    ["shift-click", { shiftKey: true }],
    ["alt-click", { altKey: true }],
    ["middle-click", { button: 1 }],
  ])("leaves %s on the external-browser path", (_label, init) => {
    const event = click(renderExternalLink(), init);

    expect(event.defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });

  it("leaves mail links to the shell's system-handler policy", () => {
    const event = click(renderExternalLink("mailto:docs@example.com"));

    expect(event.defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });

  it("keeps the default path with no conversation to scope the view to", () => {
    const event = click(renderExternalLink(LINK_URL, FILE_VIEWER_NO_CONVERSATION));

    expect(event.defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });

  it("keeps the default path on shells without the embedded browser", () => {
    // An older desktop build exposes the bridge object but predates the
    // `browser*` suite — there is no view to route into.
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: () => {},
      notify: () => Promise.resolve(false),
    };

    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });
});

describe("desktop chat link clicks with the preference at its default", () => {
  it("keeps the current external-browser behavior", () => {
    const event = click(renderExternalLink());

    expect(event.defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });
});
