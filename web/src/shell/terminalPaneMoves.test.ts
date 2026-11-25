import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { resetAppPanes } from "./appPanes";
import { pendingTerminalFocus, settleTerminalFocus } from "./terminalFocusRequest";
import { resetTerminalOpenRequestsForTests, useTerminalOpenRequests } from "./terminalOpenRequest";
import {
  dropTerminalAsPane,
  moveTerminalToSidebar,
  openTerminalAsPane,
  TERMINAL_MOVE_REFUSALS,
  type TerminalMoveScope,
  terminalPaneDropAt,
  terminalToPaneRefusal,
  terminalToSidebarRefusal,
} from "./terminalPaneMoves";
import { readPaneSplit, updatePaneSplit } from "./paneSplitStore";

const AGENT = "terminal:terminal_claude_main";
const SHELL = "terminal:terminal_zsh_a";
const OTHER = "terminal:terminal_zsh_b";

let conversation = 0;
const nextConversation = () => `conv_terminal_moves_${++conversation}`;

/** A scope where every listed shell runs, as a desktop owner with splits on sees it. */
function scope(overrides: Partial<TerminalMoveScope> = {}): TerminalMoveScope {
  return {
    agentKey: AGENT,
    splitsAllowed: true,
    isLive: (key) => [AGENT, SHELL, OTHER].includes(key),
    ...overrides,
  };
}

/** The chat (the agent's pane) with `keys` beside it, `focused` focused. */
function splitBesideChat(conversationId: string, keys: string[], focused = keys.at(-1)!) {
  updatePaneSplit(conversationId, () => ({
    layout:
      keys.length === 1
        ? { axis: "columns", ratio: 0.5, children: [AGENT, keys[0]] }
        : {
            axis: "columns",
            ratio: 0.5,
            children: [AGENT, { axis: "rows", ratio: 0.5, children: [keys[0], keys[1]] }],
          },
    focusedKey: focused,
  }));
}

const layoutKeys = (conversationId: string) => {
  const split = readPaneSplit(conversationId);
  return split ? terminalLayoutKeys(split.layout) : [];
};

/** The sidebar's shell opener, as AppShell answers it. */
function listenForSidebarOpens(conversationId: string) {
  const opened = vi.fn();
  renderHook(() => useTerminalOpenRequests(conversationId, opened));
  return opened;
}

/** The pane of `conversationId` asked for the keyboard, if one is. */
const keyboardRequest = (conversationId: string) => pendingTerminalFocus(conversationId, "main");

beforeEach(() => {
  resetTerminalOpenRequestsForTests();
});

afterEach(() => {
  resetAppPanes();
  resetTerminalOpenRequestsForTests();
  document.body.replaceChildren();
});

describe("moving a shell pane to the sidebar", () => {
  it("closes only its pane and opens its tab with the keyboard, leaving the chat's pane", () => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL, OTHER], SHELL);
    const opened = listenForSidebarOpens(conversationId);

    expect(moveTerminalToSidebar(conversationId, SHELL, scope())).toBeUndefined();

    expect(layoutKeys(conversationId)).toEqual([AGENT, OTHER]);
    expect(opened).toHaveBeenCalledExactlyOnceWith({ key: SHELL, focus: true });
  });

  it("leaves the split as the chat alone once the last shell beside it moves", () => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL]);
    listenForSidebarOpens(conversationId);

    expect(moveTerminalToSidebar(conversationId, SHELL, scope())).toBeUndefined();

    expect(layoutKeys(conversationId)).not.toContain(SHELL);
  });

  it.each([
    ["the session's own terminal", AGENT, scope(), TERMINAL_MOVE_REFUSALS.agent],
    ["an app pane", "app:files", scope(), TERMINAL_MOVE_REFUSALS.notShell],
    ["a shell no pane shows", OTHER, scope(), TERMINAL_MOVE_REFUSALS.notInPane],
    [
      "an ended shell",
      SHELL,
      scope({ isLive: (key) => key === AGENT }),
      TERMINAL_MOVE_REFUSALS.ended,
    ],
    [
      "a session without splits",
      SHELL,
      scope({ splitsAllowed: false }),
      TERMINAL_MOVE_REFUSALS.splitsOff,
    ],
  ])("refuses %s, and changes nothing", (_case, key, moveScope, refusal) => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL]);
    const opened = listenForSidebarOpens(conversationId);
    const before = readPaneSplit(conversationId);

    expect(terminalToSidebarRefusal(conversationId, key, moveScope)).toBe(refusal);
    expect(moveTerminalToSidebar(conversationId, key, moveScope)).toBe(refusal);

    expect(readPaneSplit(conversationId)).toEqual(before);
    expect(opened).not.toHaveBeenCalled();
  });

  it("refuses a lone shell with no chat beside it, which would leave nothing to show", () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({ layout: SHELL, focusedKey: SHELL }));

    expect(terminalToSidebarRefusal(conversationId, SHELL, scope())).toBe(
      TERMINAL_MOVE_REFUSALS.lastPane,
    );
  });

  it("checks again as it runs: a shell that ended after the menu opened stays put", () => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL]);
    const opened = listenForSidebarOpens(conversationId);
    let live = true;
    const moveScope = scope({ isLive: (key) => key === AGENT || live });
    expect(terminalToSidebarRefusal(conversationId, SHELL, moveScope)).toBeUndefined();

    live = false;
    expect(moveTerminalToSidebar(conversationId, SHELL, moveScope)).toBe(
      TERMINAL_MOVE_REFUSALS.ended,
    );

    expect(layoutKeys(conversationId)).toEqual([AGENT, SHELL]);
    expect(opened).not.toHaveBeenCalled();
  });
});

describe("opening a sidebar shell as a pane", () => {
  it("splits the chat's lone pane, and the shell takes focus and the keyboard", () => {
    const conversationId = nextConversation();

    expect(openTerminalAsPane(conversationId, SHELL, scope())).toBeUndefined();

    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    });
    expect(keyboardRequest(conversationId)?.tabKey).toBe(SHELL);
  });

  it("focuses a shell the split already shows instead of adding it again", () => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL, OTHER], OTHER);
    const before = readPaneSplit(conversationId)!.layout;

    expect(openTerminalAsPane(conversationId, SHELL, scope())).toBeUndefined();

    expect(readPaneSplit(conversationId)).toEqual({ layout: before, focusedKey: SHELL });
    expect(layoutKeys(conversationId).filter((key) => key === SHELL)).toHaveLength(1);
  });

  it.each([
    ["the session's own terminal", AGENT, scope(), TERMINAL_MOVE_REFUSALS.agent],
    ["an ended shell", SHELL, scope({ isLive: () => false }), TERMINAL_MOVE_REFUSALS.ended],
    [
      "a session without its terminal",
      SHELL,
      scope({ agentKey: null }),
      TERMINAL_MOVE_REFUSALS.noAgent,
    ],
    [
      "a session without splits",
      SHELL,
      scope({ splitsAllowed: false }),
      TERMINAL_MOVE_REFUSALS.splitsOff,
    ],
  ])("refuses %s, and changes nothing", (_case, key, moveScope, refusal) => {
    const conversationId = nextConversation();

    expect(terminalToPaneRefusal(conversationId, key, moveScope)).toBe(refusal);
    expect(openTerminalAsPane(conversationId, key, moveScope)).toBe(refusal);

    expect(readPaneSplit(conversationId)).toBeUndefined();
  });
});

describe("dropping a sidebar shell on the panes", () => {
  /** The session's panes laid out at 0,0 to 1000x500 client pixels. */
  function placePanes(conversationId: string) {
    const root = document.createElement("div");
    root.dataset.splitPanes = conversationId;
    root.getBoundingClientRect = () => new DOMRect(0, 0, 1000, 500);
    document.body.append(root);
  }

  it("lands on the side of the pane it meets, with the keyboard", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    const drop = terminalPaneDropAt(conversationId, SHELL, { x: 950, y: 250 }, scope());
    expect(drop).toMatchObject({ targetKey: AGENT, side: "right" });

    expect(dropTerminalAsPane(conversationId, SHELL, drop!, scope())).toBe(true);

    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    });
    expect(keyboardRequest(conversationId)?.tabKey).toBe(SHELL);
  });

  it("offers no spot for a shell that can't move, or one the panes already show", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    const point = { x: 950, y: 250 };

    expect(
      terminalPaneDropAt(conversationId, SHELL, point, scope({ isLive: () => false })),
    ).toBeNull();
    expect(terminalPaneDropAt(conversationId, AGENT, point, scope())).toBeNull();
    splitBesideChat(conversationId, [SHELL]);
    expect(terminalPaneDropAt(conversationId, SHELL, point, scope())).toBeNull();
  });

  it("checks again at the drop: a shell that ended since the preview isn't added", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    let live = true;
    const moveScope = scope({ isLive: (key) => key === AGENT || live });
    const drop = terminalPaneDropAt(conversationId, SHELL, { x: 950, y: 250 }, moveScope)!;

    live = false;
    expect(dropTerminalAsPane(conversationId, SHELL, drop, moveScope)).toBe(false);

    expect(readPaneSplit(conversationId)).toBeUndefined();
  });

  it("isn't added beside a pane that closed since the preview", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    splitBesideChat(conversationId, [OTHER]);
    const drop = { targetKey: OTHER, side: "right" as const };
    updatePaneSplit(conversationId, () => ({ layout: AGENT, focusedKey: AGENT }));

    expect(dropTerminalAsPane(conversationId, SHELL, drop, scope())).toBe(false);

    expect(layoutKeys(conversationId)).toEqual([AGENT]);
  });

  it("only focuses a shell the panes took meanwhile, never adding it twice", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    const drop = terminalPaneDropAt(conversationId, SHELL, { x: 950, y: 250 }, scope())!;
    splitBesideChat(conversationId, [SHELL, OTHER], OTHER);

    expect(dropTerminalAsPane(conversationId, SHELL, drop, scope())).toBe(true);

    expect(layoutKeys(conversationId).filter((key) => key === SHELL)).toHaveLength(1);
    expect(readPaneSplit(conversationId)?.focusedKey).toBe(SHELL);
  });
});

describe("a shell moved out and back", () => {
  it("is in exactly one place after each move", () => {
    const conversationId = nextConversation();
    splitBesideChat(conversationId, [SHELL]);
    const opened = listenForSidebarOpens(conversationId);

    for (let round = 0; round < 3; round += 1) {
      expect(moveTerminalToSidebar(conversationId, SHELL, scope())).toBeUndefined();
      expect(layoutKeys(conversationId)).not.toContain(SHELL);
      expect(openTerminalAsPane(conversationId, SHELL, scope())).toBeUndefined();
      expect(layoutKeys(conversationId).filter((key) => key === SHELL)).toHaveLength(1);
      settleTerminalFocus(conversationId, "main");
    }
    expect(opened).toHaveBeenCalledTimes(3);
  });
});
