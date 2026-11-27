// ⌘↵ on a session row in the QuickPick palette: that session's actions on a page of their own.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import {
  useRecentSessionHotkey,
  useRecentSessionSwitchStore,
} from "@/hooks/useRecentSessionSwitch";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import type { Conversation } from "@/hooks/useConversations";

import { LEVEL_READ } from "@/lib/permissionsApi";
import { openPaletteDialog } from "@/test/paletteCommands";

import { usePaletteCommands, usePaletteCommandsStore } from "@/store/paletteCommands";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";
import { OWNER_ONLY_REASONS, UNKNOWN_OWNER_REASON } from "./sessionCommands/model";
import {
  latestSessionActionRequest,
  useSessionActionStore,
} from "./sessionCommands/sessionActionStore";
import {
  SESSION_PICK_COMMANDS_HEADING,
  sessionPickCommands,
} from "./sessionCommands/useSessionPickCommands";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true, session_mute: true }),
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "viewer" }));
// Fork asks the session's host; nothing here is restricted.
vi.mock("@/hooks/useSessionActionRestrictions", () => ({
  useSessionActionRestrictions: () => ({ forkDisabledReason: undefined }),
}));
const useConversations = vi.fn();
const useSessionSearch = vi.fn((..._args: unknown[]) => ({ isFetching: false }));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const { isSessionDeleting, PINNED_CONVERSATIONS_KEY } =
    await importOriginal<typeof ConversationsModule>();
  return {
    useConversations: (...args: unknown[]) => useConversations(...args),
    useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
    useProjects: () => ({ data: [] }),
    isSessionDeleting,
    PINNED_CONVERSATIONS_KEY,
  };
});

const MINE: Conversation = {
  id: "c1",
  object: "conversation",
  title: "Fix the parser",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
  owner: "viewer",
};
const SHARED: Conversation = { ...MINE, id: "c2", title: "Review notes", owner: "someone-else" };

function listSessions(...rows: Conversation[]) {
  useConversations.mockReturnValue({ data: { pages: [{ data: rows }] }, isFetching: false });
}

/** With `closes`, it closes on `onOpenChange(false)` as the shell does; else it stays open. */
function renderPalette(queryClient = new QueryClient(), { closes = false } = {}) {
  const onOpenChange = vi.fn();
  function Palette() {
    const [open, setOpen] = useState(true);
    return (
      <QuickPickCommandPalette
        open={open}
        onOpenChange={(next) => {
          onOpenChange(next);
          if (closes) setOpen(next);
        }}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
    );
  }
  render(
    <QueryClientProvider client={queryClient}>
      <Palette />
    </QueryClientProvider>,
  );
  return { onOpenChange, input: screen.getByTestId("command-palette-input") };
}

const heading = () =>
  document.querySelector('[data-slot="quick-pick-heading"]')?.textContent ?? null;
const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent ?? "");

/** Press ⌘↵ (Ctrl+↵ off a Mac) on the first session row, the most recent one. */
function openActions(input: HTMLElement) {
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
}

beforeEach(() => {
  navigate.mockClear();
  listSessions(MINE, SHARED);
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  useSessionActionStore.setState({ request: null });
});

describe("QuickPick ⌘K session actions", () => {
  it("names ⌘↵ on a session row in the footer", () => {
    renderPalette();
    expect(screen.getByRole("button", { name: /Show actions/ })).toBeTruthy();
  });

  it("lists the session's actions on ⌘↵, as its row menu does, and goes back to the row", async () => {
    const { input, onOpenChange } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Fix the parser”"));
    expect(optionNames()).toEqual([
      "Open in new window",
      "Pin",
      "Share",
      "Fork",
      "Rename",
      "Mute",
      "Mark as unread",
      "Add to project",
      "Archive",
      "Delete",
    ]);
    expect(navigate).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(heading()).toBeNull());
    expect(input).toHaveValue("");
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active).toHaveTextContent("Fix the parser");
  });

  it("greys the owner's actions on a session shared with the viewer, saying why", async () => {
    listSessions(SHARED, MINE);
    const { input } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Review notes”"));
    const rename = screen.getByRole("option", { name: /Rename/ });
    expect(rename).toHaveAttribute("aria-disabled", "true");
    expect(rename).toHaveTextContent(OWNER_ONLY_REASONS.rename);
    expect(optionNames().at(-1)).toContain("Leave session");
  });

  it("hands an action that runs in place to the host and closes", async () => {
    const { input, onOpenChange } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).not.toBeNull());
    fireEvent.change(input, { target: { value: "mark as unread" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(latestSessionActionRequest()).toMatchObject({
      command: "mark-unread",
      conversation: { id: "c1" },
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("hands an action that opens a dialog to the host only once the palette has closed", async () => {
    const { input, onOpenChange } = renderPalette(undefined, { closes: true });
    openActions(input);
    await waitFor(() => expect(heading()).not.toBeNull());
    const paletteOpenWhenAsked: boolean[] = [];
    const asked = vi.fn(() => paletteOpenWhenAsked.push(openPaletteDialog() !== null));
    onTestFinished(useSessionActionStore.subscribe(asked));
    fireEvent.change(input, { target: { value: "rename" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    await waitFor(() => expect(asked).toHaveBeenCalledTimes(1));
    // The host is asked only once the palette is no longer open.
    expect(paletteOpenWhenAsked).toEqual([false]);
    expect(latestSessionActionRequest()).toMatchObject({
      command: "rename",
      conversation: { id: "c1" },
    });
  });

  it("keeps the page on a greyed action, asking nothing of the host", async () => {
    listSessions(SHARED, MINE);
    const { input, onOpenChange } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).not.toBeNull());
    fireEvent.change(input, { target: { value: "rename" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(latestSessionActionRequest()).toBeNull();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(heading()).toBe("Actions for “Review notes”");
  });

  it("keeps a search result's owner when only its detail is cached, and stays steady", async () => {
    const queryClient = new QueryClient();
    // The session's own fetch, which carries no owner, and no list holds it.
    queryClient.setQueryData(["session", "c2"], {
      id: "c2",
      title: "Review notes",
      createdAt: 0,
      permissionLevel: 1,
      labels: {},
      archived: false,
    });
    listSessions(SHARED, MINE);
    const { input } = renderPalette(queryClient);
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Review notes”"));
    const rename = screen.getByRole("option", { name: /Rename/ });
    expect(rename).toHaveAttribute("aria-disabled", "true");
  });

  it("keeps a search result's owner over a fetched row that can't say whose it is", async () => {
    const queryClient = new QueryClient();
    // The open session's own fetch: shared with the viewer, so it has no owner.
    queryClient.setQueryData(["conversation-backfill", "c2"], {
      ...SHARED,
      owner: null,
      permission_level: LEVEL_READ,
    });
    listSessions({ ...SHARED, permission_level: LEVEL_READ }, MINE);
    const { input } = renderPalette(queryClient);
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Review notes”"));
    expect(screen.getByRole("option", { name: /Rename/ })).toHaveAttribute("aria-disabled", "true");
    expect(optionNames().at(-1)).toContain("Leave session");
    expect(optionNames().some((name) => name.includes("Delete"))).toBe(false);
  });

  it("greys the owner's actions when a cached row can't say whose the session is", async () => {
    const queryClient = new QueryClient();
    // Pinning builds the pinned row from what the cache has, which may name no owner.
    const { PINNED_CONVERSATIONS_KEY } = await vi.importActual<typeof ConversationsModule>(
      "@/hooks/useConversations",
    );
    queryClient.setQueryData(PINNED_CONVERSATIONS_KEY, {
      conversations: [{ ...SHARED, owner: undefined, permission_level: null }],
    });
    listSessions(SHARED, MINE);
    const { input } = renderPalette(queryClient);
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Review notes”"));
    const rename = screen.getByRole("option", { name: /Rename/ });
    expect(rename).toHaveAttribute("aria-disabled", "true");
    expect(rename).toHaveTextContent(UNKNOWN_OWNER_REASON);
    const remove = screen.getByRole("option", { name: /Delete/ });
    expect(remove).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("option", { name: /Leave session/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("names the page as the session reads now, after a rename elsewhere", async () => {
    const queryClient = new QueryClient();
    const cacheSessions = (...rows: Conversation[]) =>
      queryClient.setQueryData(["conversations", "sidebar"], {
        pages: [{ data: rows }],
        pageParams: [undefined],
      });
    cacheSessions(MINE, SHARED);
    const { input } = renderPalette(queryClient);
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Fix the parser”"));
    act(() => cacheSessions({ ...MINE, title: "Parser rewrite" }, SHARED));
    await waitFor(() => expect(heading()).toBe("Actions for “Parser rewrite”"));
  });

  it("lists no actions for a session still being created", async () => {
    listSessions({ ...MINE, id: "temp:example", provisional: true }, SHARED);
    const { input } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).toBe("Actions for “Fix the parser”"));
    expect(screen.queryAllByRole("option")).toEqual([]);
    expect(screen.getByText("No actions for this session.")).toBeTruthy();
  });

  it("opens the session as before on a plain Enter", () => {
    const { input } = renderPalette();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c1");
  });

  it("follows the session's current state once its actions are open, as when it's archived", async () => {
    const queryClient = new QueryClient();
    const cacheSessions = (...rows: Conversation[]) =>
      queryClient.setQueryData(["conversations", "sidebar"], {
        pages: [{ data: rows }],
        pageParams: [undefined],
      });
    cacheSessions(MINE, SHARED);
    const { input } = renderPalette(queryClient);
    openActions(input);
    await waitFor(() => expect(optionNames()).toContain("Pin"));
    act(() => cacheSessions({ ...MINE, archived: true }, SHARED));
    await waitFor(() => expect(optionNames()).toContain("Unarchive"));
    expect(optionNames()).not.toContain("Pin");
    // Gone from every list since: nothing is offered, and nothing runs.
    act(() => cacheSessions(SHARED));
    await waitFor(() => expect(screen.queryAllByRole("option")).toEqual([]));
    fireEvent.change(input, { target: { value: "unarchive" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(latestSessionActionRequest()).toBeNull();
  });
});

describe("QuickPick ⌘K verb-first session actions", () => {
  // As the session action host does with quick_pick_palette on.
  function PickRows() {
    usePaletteCommands("session-picks", SESSION_PICK_COMMANDS_HEADING, sessionPickCommands());
    return null;
  }
  function renderWithPicks() {
    render(<PickRows />);
    // It closes as the shell does, so an action that opens a dialog reaches its host.
    return renderPalette(undefined, { closes: true });
  }
  afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));
  const activeRow = (input: HTMLElement) =>
    document.getElementById(input.getAttribute("aria-activedescendant") ?? "");

  it("asks which session, then opens its actions on the one the verb named", async () => {
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "archive a session" } });
    await screen.findByRole("option", { name: /Archive a Session…/ });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Archive which session?"));
    expect(input).toHaveValue("");
    expect(optionNames()).toEqual([
      expect.stringContaining("Fix the parser"),
      expect.stringContaining("Review notes"),
    ]);

    fireEvent.change(input, { target: { value: "parser" } });
    await waitFor(() => expect(optionNames()).toEqual([expect.stringContaining("Fix the parser")]));
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Actions for “Fix the parser”"));
    await waitFor(() => expect(activeRow(input)).toHaveTextContent("Archive"));
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(latestSessionActionRequest()).toMatchObject({
        command: "archive",
        conversation: { id: "c1" },
      }),
    );
  });

  it("says when the session doesn't offer the verb's action", async () => {
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "unpin a session" } });
    await screen.findByRole("option", { name: /Unpin a Session…/ });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Unpin which session?"));
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Actions for “Fix the parser”"));
    expect(document.querySelector('[data-slot="palette-page-status"]')).toHaveTextContent(
      "Unpin isn't available for “Fix the parser”.",
    );
    expect(optionNames()).not.toContain("Unpin");
  });

  it("lists archived sessions only for a verb that applies to them, such as Unarchive", async () => {
    const archived: Conversation = { ...MINE, id: "c3", title: "Old spike", archived: true };
    listSessions(MINE, archived);
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "archive a session" } });
    fireEvent.click(await screen.findByRole("option", { name: /^Archive a Session…/ }));
    await waitFor(() => expect(heading()).toBe("Archive which session?"));
    expect(optionNames().some((name) => name.includes("Old spike"))).toBe(false);

    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(heading()).toBeNull());
    fireEvent.change(input, { target: { value: "unarchive a session" } });
    fireEvent.click(await screen.findByRole("option", { name: /^Unarchive a Session…/ }));
    await waitFor(() => expect(heading()).toBe("Unarchive which session?"));
    // The list it reads asks the server for archived sessions too.
    expect(useConversations).toHaveBeenLastCalledWith("", true, expect.anything());
    expect(optionNames().some((name) => name.includes("Old spike"))).toBe(true);
  });

  it.each(["#42", "https://github.com/acme/app/pull/42"])(
    "finds an archived session from its pull request %s in Unarchive's picker, not Archive's",
    async (reference) => {
      const archived = {
        ...MINE,
        id: "c3",
        title: "Old spike",
        archived: true,
        pull_request: {
          url: "https://github.com/acme/app/pull/42",
          provider: "github",
          repository: "acme/app",
          number: 42,
        },
      } as Conversation;
      listSessions(MINE, archived);
      const { input } = renderWithPicks();
      const pickFor = async (verb: string, question: string) => {
        fireEvent.change(input, { target: { value: verb.toLowerCase() } });
        fireEvent.click(await screen.findByRole("option", { name: new RegExp(`^${verb}`) }));
        await waitFor(() => expect(heading()).toBe(question));
        fireEvent.change(input, { target: { value: reference } });
      };
      await pickFor("Unarchive a Session…", "Unarchive which session?");
      await waitFor(() => expect(optionNames()).toEqual([expect.stringContaining("Old spike")]));

      fireEvent.keyDown(input, { key: "Escape" });
      await waitFor(() => expect(heading()).toBeNull());
      await pickFor("Archive a Session…", "Archive which session?");
      await waitFor(() =>
        expect(
          screen.queryAllByRole("option").some((row) => row.textContent?.includes("Old spike")),
        ).toBe(false),
      );
    },
  );

  it("searches the server from the session picker, so a session not loaded yet can be found", async () => {
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "rename a session" } });
    fireEvent.click(await screen.findByRole("option", { name: /^Rename a Session…/ }));
    await waitFor(() => expect(heading()).toBe("Rename which session?"));
    fireEvent.change(input, { target: { value: "quarterly" } });
    await waitFor(() =>
      expect(useSessionSearch).toHaveBeenCalledWith(
        "quarterly",
        expect.anything(),
        expect.objectContaining({ enabled: true }),
      ),
    );
  });

  it("goes back from the session picker to the row that opened it, with its query", async () => {
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "rename a session" } });
    await screen.findByRole("option", { name: /Rename a Session…/ });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Rename which session?"));
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(heading()).toBeNull());
    expect(input).toHaveValue("rename a session");
    await waitFor(() => expect(activeRow(input)).toHaveTextContent("Rename a Session…"));
  });

  it("goes back from a picked session's actions to the picker, on that session", async () => {
    const { input } = renderWithPicks();
    fireEvent.change(input, { target: { value: "archive a session" } });
    await screen.findByRole("option", { name: /Archive a Session…/ });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Archive which session?"));
    fireEvent.change(input, { target: { value: "notes" } });
    await waitFor(() => expect(optionNames()).toEqual([expect.stringContaining("Review notes")]));
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Actions for “Review notes”"));

    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(heading()).toBe("Archive which session?"));
    expect(input).toHaveValue("notes");
    await waitFor(() => expect(activeRow(input)).toHaveTextContent("Review notes"));
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(heading()).toBeNull());
    expect(input).toHaveValue("archive a session");
  });
});

describe("QuickPick ⌘K and the held recent-session switcher", () => {
  function RecentSessionChord() {
    useRecentSessionHotkey(true);
    return null;
  }

  afterEach(() => {
    useRecentSessionSwitchStore.setState({ history: [], switcher: null, chordOwner: null });
    useSidebarSessionOrderStore.setState({ orderedSessionIds: [], activeSessionId: null });
  });

  it("closes on ⌃] and opens the switcher in its place, which opens its pick on release", async () => {
    const live = new Set(["c1", "c2"]);
    useSidebarSessionOrderStore.setState({
      orderedSessionIds: ["c1", "c2"],
      activeSessionId: "c1",
    });
    useRecentSessionSwitchStore.setState({
      history: ["c2", "c1"],
      switcher: null,
      chordOwner: null,
      readLiveSessionIds: () => live,
      sessionsKnown: true,
    });
    render(<RecentSessionChord />);
    const { onOpenChange, input } = renderPalette(new QueryClient(), { closes: true });
    input.focus();

    const chord = new KeyboardEvent("keydown", {
      key: "]",
      code: "BracketRight",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      input.dispatchEvent(chord);
    });
    expect(chord.defaultPrevented).toBe(true);
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
    await waitFor(() => expect(openPaletteDialog()).toBeNull());
    expect(useRecentSessionSwitchStore.getState().switcher).toMatchObject({ selectedId: "c2" });

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keyup", { key: "Control", bubbles: true }));
    });
    expect(useRecentSessionSwitchStore.getState().switcher).toBeNull();
    expect(navigate).toHaveBeenCalledWith("/c/c2");
  });
});
