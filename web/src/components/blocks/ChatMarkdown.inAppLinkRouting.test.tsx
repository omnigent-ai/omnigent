// Desktop chat links: an opted-in plain click routes to the embedded browser;
// everything else keeps the shell's external default.

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

it("routes an opted-in plain click into the conversation's browser view", () => {
  expect(clickLink().defaultPrevented).toBe(true);
  expect(openOrNavigate).toHaveBeenCalledWith("conv-1", LINK, undefined, { agent: true });
});

it.each([
  ["cmd-click", LINK, { metaKey: true }],
  ["ctrl-click", LINK, { ctrlKey: true }],
  ["middle-click", LINK, { button: 1 }],
  ["a mailto: link", "mailto:a@example.com", {}],
])("keeps %s on the external path", (_label, href, init) => {
  expect(clickLink(href, init).defaultPrevented).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it("keeps the external default when the preference is off", () => {
  writeOpenLinksInApp(false);
  expect(clickLink().defaultPrevented).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});
