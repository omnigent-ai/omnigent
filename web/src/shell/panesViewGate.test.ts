import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setChatPaneBodyHost, setSplitBesideChat } from "./chatPaneToolbarHost";
import { useChatInPanes, usePanesSplitsAllowed, usePanesView } from "./panesViewGate";

const gate = vi.hoisted(() => ({ tabsSidebar: true, mobile: false }));
vi.mock("./useTabsSidebar", () => ({ useTabsSidebar: () => gate.tabsSidebar }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => gate.mobile }));

const SESSION = "conv_panes_gate";

afterEach(() => {
  gate.tabsSidebar = true;
  gate.mobile = false;
  setSplitBesideChat(SESSION, false);
  setChatPaneBodyHost(SESSION, null);
});

const panes = () => renderHook(() => usePanesView()).result.current;
const splits = (readOnly: boolean) =>
  renderHook(() => usePanesSplitsAllowed(readOnly)).result.current;

describe("panes view gate", () => {
  it("shows the panes view with the tabs sidebar on a desktop window, and lets owners split", () => {
    expect(panes()).toBe(true);
    expect(splits(false)).toBe(true);
    expect(splits(true)).toBe(false);
  });

  it.each([
    ["the tabs sidebar is off", { tabsSidebar: false }],
    ["the window is phone-sized", { mobile: true }],
  ])("shows no panes view when %s", (_case, state) => {
    Object.assign(gate, state);
    expect(panes()).toBe(false);
    expect(splits(false)).toBe(false);
  });
});

describe("the chat in its pane", () => {
  const host = document.createElement("div");
  const chatIn = (chatShown: boolean) =>
    renderHook(() => useChatInPanes(SESSION, chatShown)).result.current;

  it("renders the chat in its pane while Chat shows beside a split", () => {
    setSplitBesideChat(SESSION, true);
    setChatPaneBodyHost(SESSION, host);
    expect(chatIn(true)).toEqual({ inPanes: true, host, marked: true });
  });

  it("keeps the panes on screen before the chat pane has mounted, the chat in its column", () => {
    setSplitBesideChat(SESSION, true);
    expect(chatIn(true)).toEqual({ inPanes: true, host: null, marked: true });
  });

  it.each([
    [
      "no split shows beside the chat, marked for the pane keys",
      { split: false, chatShown: true, tabsSidebar: true, marked: true },
    ],
    [
      "the session shows its Terminal view",
      { split: true, chatShown: false, tabsSidebar: true, marked: false },
    ],
    [
      "the panes view is off, unmarked",
      { split: true, chatShown: true, tabsSidebar: false, marked: false },
    ],
  ])("keeps the chat in its column when %s", (_case, { split, chatShown, tabsSidebar, marked }) => {
    gate.tabsSidebar = tabsSidebar;
    setSplitBesideChat(SESSION, split);
    setChatPaneBodyHost(SESSION, host);
    expect(chatIn(chatShown)).toEqual({ inPanes: false, host: null, marked });
  });

  it("follows the split as it appears and goes", () => {
    setChatPaneBodyHost(SESSION, host);
    const { result } = renderHook(() => useChatInPanes(SESSION, true));
    expect(result.current.inPanes).toBe(false);
    act(() => setSplitBesideChat(SESSION, true));
    expect(result.current).toEqual({ inPanes: true, host, marked: true });
    act(() => setSplitBesideChat(SESSION, false));
    expect(result.current.inPanes).toBe(false);
  });
});
