// ⌘↵ on a session row in the QuickPick palette: that session's actions on a page of their own.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";
import { OWNER_ONLY_REASONS } from "./sessionCommands/model";
import {
  latestSessionActionRequest,
  useSessionActionStore,
} from "./sessionCommands/sessionActionStore";

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
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const { isSessionDeleting, PINNED_CONVERSATIONS_KEY } =
    await importOriginal<typeof ConversationsModule>();
  return {
    useConversations: (...args: unknown[]) => useConversations(...args),
    useSessionSearch: () => ({ isFetching: false }),
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
    const asked = vi.fn();
    onTestFinished(useSessionActionStore.subscribe(asked));
    fireEvent.change(input, { target: { value: "rename" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    // The dialog is still closing, so the host hasn't been asked yet.
    expect(asked).not.toHaveBeenCalled();
    await waitFor(() => expect(asked).toHaveBeenCalledTimes(1));
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
