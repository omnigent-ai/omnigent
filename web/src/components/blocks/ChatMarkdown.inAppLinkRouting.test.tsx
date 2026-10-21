// Desktop chat links: a same-origin session link opens in place, an opted-in
// plain click routes to the embedded browser, and everything else keeps the
// shell's external default.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import type * as NativeBridge from "@/lib/nativeBridge";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => true,
}));

const LINK = "https://example.com/page";
const SESSION_LINK = `${window.location.origin}/c/conv-2`;
let openOrNavigate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  openOrNavigate = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: { kind: "electron", browserOpenOrNavigate: openOrNavigate },
  });
  writeOpenLinksInApp(true);
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

const FILE_VIEWER = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: "conv-1",
  workspaceRoot: "/ws",
  workspaceHome: "/home",
};

function clickLink(href = LINK, init: MouseEventInit = {}): MouseEvent {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        <FilePathAwareMessageResponse>{`[docs](${href})`}</FilePathAwareMessageResponse>
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init });
  screen.getByText("docs").dispatchEvent(event);
  return event;
}

it("routes an opted-in plain click into the conversation's browser view", () => {
  expect(clickLink().defaultPrevented).toBe(true);
  expect(openOrNavigate).toHaveBeenCalledWith("conv-1", LINK, undefined, { agent: true });
});

it.each([
  ["cmd-click", LINK, { metaKey: true }],
  ["ctrl-click", LINK, { ctrlKey: true }],
  ["middle-click", LINK, { button: 1 }],
  ["a mailto: link", "mailto:a@example.com", {}],
  ["cmd-click on a session link", SESSION_LINK, { metaKey: true }],
])("keeps %s on the external path", (_label, href, init) => {
  expect(clickLink(href, init).defaultPrevented).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([LINK, "https://other.example.com/c/conv-2"])(
  "keeps %s on the external path when the preference is off",
  (href) => {
    writeOpenLinksInApp(false);
    window.history.replaceState(null, "", "/c/conv-1");
    expect(clickLink(href).defaultPrevented).toBe(false);
    expect(window.location.pathname).toBe("/c/conv-1");
    expect(openOrNavigate).not.toHaveBeenCalled();
  },
);

it.each([true, false])(
  "opens a same-origin session link in place (in-app browser preference %s)",
  (preference) => {
    writeOpenLinksInApp(preference);
    window.history.replaceState(null, "", "/c/conv-1");
    expect(clickLink(SESSION_LINK).defaultPrevented).toBe(true);
    expect(window.location.pathname).toBe("/c/conv-2");
    expect(openOrNavigate).not.toHaveBeenCalled();
  },
);

it.each([true, false])(
  "keeps a same-origin non-session link off the session path (in-app browser preference %s)",
  (preference) => {
    writeOpenLinksInApp(preference);
    window.history.replaceState(null, "", "/c/conv-1");
    const settingsLink = `${window.location.origin}/settings/c/session`;
    expect(clickLink(settingsLink).defaultPrevented).toBe(preference);
    expect(window.location.pathname).toBe("/c/conv-1");
    expect(openOrNavigate.mock.calls).toEqual(
      preference ? [["conv-1", settingsLink, undefined, { agent: true }]] : [],
    );
  },
);
