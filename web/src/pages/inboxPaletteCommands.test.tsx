import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  chooseSettingValue,
  expectSetting,
  registeredPaletteCommands,
} from "@/test/paletteCommands";

import type { InboxEntry } from "./InboxPage";
import {
  INBOX_FILTER_SETTING_LABEL,
  InboxPaletteCommands,
  type InboxPaletteHandlers,
  type InboxPaletteState,
  inboxPaletteCommands,
  REPLY_SENDING_REASON,
} from "./inboxPaletteCommands";

const row = (id: string, title: string, updatedAt = 10) => ({
  id,
  object: "conversation",
  title,
  created_at: 1,
  updated_at: updatedAt,
  labels: {},
  permission_level: null,
});

// Only the fields the commands read; the page builds the full entries.
const approval = {
  kind: "approval",
  key: "approval:e1",
  at: 1,
  item: { row: row("s1", "Fix the parser") },
} as unknown as InboxEntry;
const fileComment = {
  kind: "comment",
  key: "comment:c1",
  at: 1,
  item: {
    row: row("s2", "Review docs"),
    comment: { id: "c1", path: "docs/a b.md", anchor_metadata: null },
  },
} as unknown as InboxEntry;
const unread = (title = "Nightly build", updatedAt = 10) =>
  ({
    kind: "unread",
    key: "unread:s3",
    at: 1,
    item: { row: row("s3", title, updatedAt) },
  }) as unknown as InboxEntry;
const automation = {
  kind: "automation",
  key: "automation:r1",
  at: 1,
  item: { runId: "r1", taskId: "t1", taskName: "Daily digest" },
} as unknown as InboxEntry;
const suggestion = {
  kind: "suggestion",
  key: "suggestion:x",
  at: 1,
  item: {},
} as unknown as InboxEntry;

const TABS: InboxPaletteState["tabs"] = [
  { value: "all", label: "All" },
  { value: "unread", label: "Unread" },
  { value: "awaiting", label: "Awaiting response" },
];

function state(overrides: Partial<InboxPaletteState> = {}): InboxPaletteState {
  return {
    filter: "all",
    tabs: TABS,
    entries: [approval, fileComment, unread(), automation, suggestion],
    taskDetailsEnabled: true,
    replySending: () => false,
    canReply: () => false,
    ...overrides,
  };
}

function handlers(): InboxPaletteHandlers {
  return {
    chooseFilter: vi.fn(),
    navigate: vi.fn(),
    markUnreadRead: vi.fn(),
    markAutomationRead: vi.fn(),
    replyTo: vi.fn(),
  };
}

const build = (overrides: Partial<InboxPaletteState> = {}, run = handlers()) => {
  const current = state(overrides);
  const commands = inboxPaletteCommands(current, { current: run }, { current: [] });
  return { commands, run };
};
const labels = (commands: readonly PaletteCommand[]) => commands.map(({ label }) => label);

describe("inboxPaletteCommands", () => {
  it("lists the filter, then each card's actions in the page's order, and none for suggestions", () => {
    expect(labels(build().commands)).toEqual([
      INBOX_FILTER_SETTING_LABEL,
      "Open “Fix the parser” to respond",
      "Open file in “Review docs”",
      "Open “Nightly build”",
      "Mark “Nightly build” as read",
      "Open automation “Daily digest”",
      "Mark failed “Daily digest” as read",
    ]);
  });

  it("offers the tabs the page shows, and picks one", async () => {
    const { commands, run } = build({
      tabs: [...TABS, { value: "advisor", label: "Omni Advisor" }],
      filter: "unread",
    });
    const filter = expectSetting(commands, INBOX_FILTER_SETTING_LABEL, {
      current: "Unread",
      choices: ["All", "Unread", "Awaiting response", "Omni Advisor"],
    });
    await chooseSettingValue(filter, "Awaiting response");
    expect(run.chooseFilter).toHaveBeenCalledWith("awaiting");
  });

  it("greys an unread card's actions while its reply sends", () => {
    const { commands } = build({ replySending: (id) => id === "s3" });
    for (const label of ["Open “Nightly build”", "Mark “Nightly build” as read"]) {
      expect(commands.find((command) => command.label === label)?.availability).toEqual({
        available: false,
        reason: REPLY_SENDING_REASON,
      });
    }
  });

  it("offers Reply on an unread card only where its reply field shows, greyed while one sends", () => {
    const replyLabel = "Reply to “Nightly build”…";
    expect(labels(build().commands)).not.toContain(replyLabel);
    const { commands } = build({ canReply: (id) => id === "s3" });
    const reply = commands.find((command) => command.label === replyLabel);
    expect(reply).toMatchObject({ movesFocus: true, availability: { available: true } });
    const sending = build({ canReply: () => true, replySending: () => true }).commands;
    expect(sending.find((command) => command.label === replyLabel)?.availability).toEqual({
      available: false,
      reason: REPLY_SENDING_REASON,
    });
  });

  it("names a session comment's action Open, as its card does", () => {
    const sessionComment = {
      ...fileComment,
      item: {
        ...(fileComment as { item: object }).item,
        comment: { id: "c2", path: "x", anchor_metadata: {} },
      },
    } as unknown as InboxEntry;
    expect(labels(build({ entries: [sessionComment] }).commands)).toEqual([
      INBOX_FILTER_SETTING_LABEL,
      "Open “Review docs”",
    ]);
  });
});

describe("InboxPaletteCommands", () => {
  afterEach(() => {
    cleanup();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  const mount = (props: InboxPaletteState & { handlers: InboxPaletteHandlers }, flag = true) => (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
    >
      <InboxPaletteCommands {...props} />
    </CapabilitiesProvider>
  );
  const command = (label: string) =>
    registeredPaletteCommands().find((candidate) => candidate.label === label)!;

  it("runs each card's handlers: open links, unread opens are reads, and both mark-reads", () => {
    const run = handlers();
    render(mount({ ...state(), handlers: run }));
    act(() => command("Open file in “Review docs”").run());
    expect(run.navigate).toHaveBeenLastCalledWith("/c/s2?file=docs%2Fa%20b.md&comment=c1");
    act(() => command("Open “Nightly build”").run());
    expect(run.markUnreadRead).toHaveBeenCalledWith(expect.objectContaining({ key: "unread:s3" }));
    expect(run.navigate).toHaveBeenLastCalledWith("/c/s3");
    act(() => command("Mark failed “Daily digest” as read").run());
    expect(run.markAutomationRead).toHaveBeenCalledWith("r1");
    act(() => command("Open automation “Daily digest”").run());
    expect(run.navigate).toHaveBeenLastCalledWith("/tasks/t1");
  });

  it("runs Reply as the card's reply handler, without reading or leaving the page", () => {
    const run = handlers();
    render(mount({ ...state({ canReply: () => true }), handlers: run }));
    act(() => command("Reply to “Nightly build”…").run());
    expect(run.replyTo).toHaveBeenCalledWith(expect.objectContaining({ key: "unread:s3" }));
    expect(run.markUnreadRead).not.toHaveBeenCalled();
    expect(run.navigate).not.toHaveBeenCalled();
  });

  it("keeps its rows across re-renders with the same cards, and runs the newest handlers", () => {
    const { rerender } = render(mount({ ...state(), handlers: handlers() }));
    const listed = command("Mark “Nightly build” as read");
    const latest = handlers();
    // The page rebuilds its entries each render; the same cards keep the same rows.
    rerender(mount({ ...state(), handlers: latest }));
    expect(command(listed.label)).toBe(listed);
    act(() => listed.run());
    expect(latest.markUnreadRead).toHaveBeenCalledOnce();
  });

  it("does nothing for a row accepted after its card left or began sending a reply", () => {
    const run = handlers();
    const { rerender } = render(mount({ ...state(), handlers: run }));
    const listed = command("Mark “Nightly build” as read");
    rerender(mount({ ...state({ replySending: (id) => id === "s3" }), handlers: run }));
    act(() => listed.run());
    rerender(mount({ ...state({ entries: [approval] }), handlers: run }));
    act(() => listed.run());
    expect(run.markUnreadRead).not.toHaveBeenCalled();
  });

  it("does nothing for a row accepted after its unread card got new output", () => {
    const run = handlers();
    const { rerender } = render(mount({ ...state(), handlers: run }));
    const markRead = command("Mark “Nightly build” as read");
    const open = command("Open “Nightly build”");
    // The session wrote more since the rows were listed; reading it now would hide that output.
    rerender(
      mount({
        ...state({ entries: [approval, fileComment, unread("Nightly build", 20)] }),
        handlers: run,
      }),
    );
    act(() => markRead.run());
    act(() => open.run());
    expect(run.markUnreadRead).not.toHaveBeenCalled();
    expect(run.navigate).not.toHaveBeenCalled();
  });

  it("registers nothing with quick_pick_palette off", () => {
    render(mount({ ...state(), handlers: handlers() }, false));
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
