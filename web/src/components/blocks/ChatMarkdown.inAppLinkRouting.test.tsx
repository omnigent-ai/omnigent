// Desktop chat links open independent in-app tabs when opted in.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, type ReactNode } from "react";
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

function TestFileViewer({
  children,
  openGoogleDoc,
}: {
  children: ReactNode;
  openGoogleDoc?: (url: string) => boolean;
}) {
  const value = useMemo(() => ({ ...FILE_VIEWER, openGoogleDoc }), [openGoogleDoc]);
  return <FileViewerContext.Provider value={value}>{children}</FileViewerContext.Provider>;
}

function clickLink(
  href = LINK,
  init: MouseEventInit = {},
  openGoogleDoc?: (url: string) => boolean,
): MouseEvent {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TestFileViewer openGoogleDoc={openGoogleDoc}>
        <FilePathAwareMessageResponse>{`[docs](${href})`}</FilePathAwareMessageResponse>
      </TestFileViewer>
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

const DOC = "https://docs.google.com/document/d/example/edit";

it("opens a plain Doc in the session pane while preserving its original href", () => {
  const openGoogleDoc = vi.fn().mockReturnValue(true);
  const original = DOC + "?usp=sharing#tab=t.one";
  expect(clickLink(original, {}, openGoogleDoc).defaultPrevented).toBe(true);
  expect(openGoogleDoc).toHaveBeenCalledWith(DOC + "?tab=t.one");
  expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("href", original);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([
  ["heading", DOC + "#heading=h.one", {}],
  ["comment", DOC + "?disco=comment-one", {}],
  ["access key", DOC + "?resourcekey=example-key", {}],
  ["unknown target", DOC + "#unknown", {}],
  ["cmd-click", DOC, { metaKey: true }],
  ["ctrl-click", DOC, { ctrlKey: true }],
  ["shift-click", DOC, { shiftKey: true }],
  ["alt-click", DOC, { altKey: true }],
  ["middle-click", DOC, { button: 1 }],
])("keeps a Google Doc %s external with its full target", (_label, href, init) => {
  const openGoogleDoc = vi.fn().mockReturnValue(true);
  expect(clickLink(href, init, openGoogleDoc).defaultPrevented).toBe(false);
  expect(openGoogleDoc).not.toHaveBeenCalled();
  expect(openOrNavigate).not.toHaveBeenCalled();
  expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("href", href);
});

it("leaves an unhandled Doc external when the shell cannot change the reference", () => {
  const openGoogleDoc = vi.fn().mockReturnValue(false);
  expect(clickLink(DOC, {}, openGoogleDoc).defaultPrevented).toBe(false);
  expect(openGoogleDoc).toHaveBeenCalledWith(DOC);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it("retains existing desktop routing when the Google Docs capability is absent", async () => {
  expect(clickLink(DOC).defaultPrevented).toBe(true);
  await vi.waitFor(() => expect(openOrNavigate).toHaveBeenCalled());
});
