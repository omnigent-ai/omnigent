// Desktop chat links open independent in-app tabs when opted in.

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

it.each([{}, { metaKey: true }, { ctrlKey: true }])(
  "keeps the external default when the preference is off (%j)",
  (init) => {
    writeOpenLinksInApp(false);
    expect(clickLink(LINK, init).defaultPrevented).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  },
);
