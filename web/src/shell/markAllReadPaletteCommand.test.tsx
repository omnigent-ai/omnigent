import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type * as UnseenModule from "@/hooks/useUnseenConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as RoutingModule from "@/lib/routing";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import {
  MarkAllReadPaletteCommand,
  markAllReadLabel,
  type RowReadState,
  unreadSessionMarks,
} from "./markAllReadPaletteCommand";

const loaded = vi.hoisted(() => ({
  rows: [] as Conversation[],
  activeId: undefined as string | undefined,
  activeRootId: null as string | null,
}));
const markRead = vi.hoisted(() => vi.fn());
const unseen = vi.hoisted(() => ({ ids: new Set<string>() }));

vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({
    data: { pages: [{ data: loaded.rows }], pageParams: [undefined] },
  }),
}));
vi.mock("@/lib/routing", async (importOriginal) => ({
  ...(await importOriginal<typeof RoutingModule>()),
  useParams: () => ({ conversationId: loaded.activeId }),
}));
vi.mock("@/hooks/useSession", () => ({
  useActiveRootSessionId: () => loaded.activeRootId,
}));
vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UnseenModule>()),
  isConversationUnseen: (id: string) => unseen.ids.has(id),
  isExplicitlyUnread: () => false,
  markConversationRead: markRead,
  useUnseenTick: () => 0,
}));

function row(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: 10,
    labels: {},
    permission_level: null,
    ...overrides,
  } as Conversation;
}

const readState = (overrides: Partial<RowReadState> = {}): RowReadState => ({
  isUnseen: () => true,
  isExplicitlyUnread: () => false,
  showsUnread: (_row, isUnseen) => isUnseen,
  ...overrides,
});

describe("unreadSessionMarks", () => {
  it("marks each loaded row with a dot once, at its version", () => {
    expect(
      unreadSessionMarks([row("a"), row("b", { updated_at: 20 }), row("a")], [], readState()),
    ).toEqual([
      { conversationId: "a", updatedAt: 10 },
      { conversationId: "b", updatedAt: 20 },
    ]);
  });

  it("skips seen, muted and archived rows", () => {
    const rows = [row("seen"), row("muted"), row("archived", { archived: true }), row("dot")];
    const marks = unreadSessionMarks(
      rows,
      [],
      readState({
        isUnseen: (id) => id !== "seen",
        showsUnread: (candidate, isUnseen) => isUnseen && candidate.id !== "muted",
      }),
    );
    expect(marks.map(({ conversationId }) => conversationId)).toEqual(["dot"]);
  });

  it("skips the open rows unless the user marked them unread", () => {
    const rows = [row("child"), row("root"), row("other")];
    expect(unreadSessionMarks(rows, ["child", "root"], readState())).toEqual([
      { conversationId: "other", updatedAt: 10 },
    ]);
    expect(
      unreadSessionMarks(
        [row("root")],
        ["child", "root"],
        readState({ isExplicitlyUnread: () => true }),
      ),
    ).toEqual([{ conversationId: "root", updatedAt: 10 }]);
  });
});

describe("markAllReadLabel", () => {
  it("names one session, or counts them", () => {
    expect(markAllReadLabel(1)).toBe("Mark 1 unread session as read");
    expect(markAllReadLabel(1200)).toBe(
      `Mark all ${(1200).toLocaleString()} unread sessions as read`,
    );
  });
});

describe("MarkAllReadPaletteCommand", () => {
  afterEach(() => {
    cleanup();
    usePaletteCommandsStore.setState({ groups: {} });
    markRead.mockReset();
    unseen.ids.clear();
    loaded.rows = [];
    loaded.activeId = undefined;
    loaded.activeRootId = null;
  });

  const mount = (flag = true) => (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
    >
      <MarkAllReadPaletteCommand />
    </CapabilitiesProvider>
  );

  it("marks every unread loaded session read, as the rows show now", () => {
    loaded.rows = [row("a"), row("b"), row("c")];
    unseen.ids = new Set(["a", "b"]);
    const { rerender } = render(mount());
    const command = registeredPaletteCommands().find(({ label }) => label.startsWith("Mark all"))!;
    expect(command.label).toBe("Mark all 2 unread sessions as read");

    // A newer version of a row reaches the list before the command runs.
    loaded.rows = [row("a", { updated_at: 30 }), row("b"), row("c")];
    rerender(mount());
    act(() => command.run());
    expect(markRead.mock.calls).toEqual([
      ["a", 30],
      ["b", 10],
    ]);
  });

  it("skips the sidebar's open row while a nested session of its family is on screen", () => {
    loaded.rows = [row("root"), row("other")];
    unseen.ids = new Set(["root", "other"]);
    loaded.activeId = "child";
    loaded.activeRootId = "root";
    render(mount());
    const command = registeredPaletteCommands().find(({ label }) => label.startsWith("Mark"))!;
    expect(command.label).toBe("Mark 1 unread session as read");
    act(() => command.run());
    expect(markRead.mock.calls).toEqual([["other", 10]]);
  });

  it("lists nothing while no loaded session is unread", () => {
    loaded.rows = [row("a")];
    render(mount());
    expect(registeredPaletteCommands()).toEqual([]);
  });

  it("registers nothing with quick_pick_palette off", () => {
    loaded.rows = [row("a")];
    unseen.ids = new Set(["a"]);
    render(mount(false));
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
