// Desktop chat links: a plain click on a same-origin session link opens it in
// place; other links open independent in-app tabs when opted in.

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

vi.mock("@/lib/fetchSessionHostId", () => ({
  fetchSessionHostId: vi.fn().mockResolvedValue("local-host"),
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

it.each([
  ["plain click", {}],
  ["cmd-click", { metaKey: true }],
  ["ctrl-click", { ctrlKey: true }],
])("routes an opted-in %s into a new in-app browser tab", async (_label, init) => {
  expect(clickLink(LINK, init).defaultPrevented).toBe(true);
  await vi.waitFor(() =>
    expect(openOrNavigate).toHaveBeenCalledWith(
      expect.stringMatching(/^browser-tab:conv-1:[^:]+$/),
      LINK,
      undefined,
      { agent: true, sourceHostId: "local-host", sessionHostProtocol: 1 },
    ),
  );
});

it.each([
  ["shift-click", LINK, { shiftKey: true }],
  ["shift-cmd-click", LINK, { shiftKey: true, metaKey: true }],
  ["shift-ctrl-click", LINK, { shiftKey: true, ctrlKey: true }],
  ["alt-click", LINK, { altKey: true }],
  ["middle-click", LINK, { button: 1 }],
  ["a mailto: link", "mailto:a@example.com", {}],
])("keeps %s on the external path", (_label, href, init) => {
  expect(clickLink(href, init).defaultPrevented).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([
  [LINK, {}],
  [LINK, { metaKey: true }],
  [LINK, { ctrlKey: true }],
  ["https://other.example.com/c/conv-2", {}],
  [SESSION_LINK, { metaKey: true }],
  [SESSION_LINK, { ctrlKey: true }],
])("keeps %s (%j) on the external path when the preference is off", (href, init) => {
  writeOpenLinksInApp(false);
  window.history.replaceState(null, "", "/c/conv-1");
  expect(clickLink(href, init).defaultPrevented).toBe(false);
  expect(window.location.pathname).toBe("/c/conv-1");
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([true, false])(
  "opens a same-origin session link in place (in-app browser preference %s)",
  async (preference) => {
    writeOpenLinksInApp(preference);
    window.history.replaceState(null, "", "/c/conv-1");
    expect(clickLink(SESSION_LINK).defaultPrevented).toBe(true);
    expect(window.location.pathname).toBe("/c/conv-2");
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(openOrNavigate).not.toHaveBeenCalled();
  },
);

it.each([true, false])(
  "keeps a same-origin non-session link off the session path (in-app browser preference %s)",
  async (preference) => {
    writeOpenLinksInApp(preference);
    window.history.replaceState(null, "", "/c/conv-1");
    const settingsLink = `${window.location.origin}/settings/c/session`;
    expect(clickLink(settingsLink).defaultPrevented).toBe(preference);
    expect(window.location.pathname).toBe("/c/conv-1");
    await vi.waitFor(() =>
      expect(openOrNavigate.mock.calls.map(([, url]) => url)).toEqual(
        preference ? [settingsLink] : [],
      ),
    );
  },
);
