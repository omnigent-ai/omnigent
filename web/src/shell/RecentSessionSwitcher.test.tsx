// The switcher dialog over the real QuickPick, driven by the store the ⌃] / ⌃[
// binding and ⌘K share.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  openRecentSessionSwitcher,
  stepRecentSessionSwitcher,
  useRecentSessionHotkey,
  useRecentSessionSwitchStore,
} from "@/hooks/useRecentSessionSwitch";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { conversation } from "@/test/sidebarMockHelpers";

import { QuickPickShortcutHost } from "./quickPickShortcuts/QuickPickShortcutHost";
import { RecentSessionSwitcher } from "./RecentSessionSwitcher";

const navigate = vi.fn();
const MINE_SCOPE_KEY = ["conversations", "", false, null, "mine"];
let pathname = "";

/** The real recency binding, as RecentSessionSwitchHost mounts it. */
function SidebarBindings() {
  pathname = useLocation().pathname;
  useRecentSessionHotkey(true);
  return null;
}

function renderSwitcher() {
  const client = new QueryClient();
  client.setQueryData(MINE_SCOPE_KEY, {
    pages: [
      {
        data: ["a", "b", "c"].map((id) => conversation(id, { title: `Session ${id}` })),
        first_id: null,
        last_id: null,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/c/a"]}>
          <SidebarBindings />
          <QuickPickShortcutHost />
          <RecentSessionSwitcher />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return client;
}

function key(type: "keydown" | "keyup", init: KeyboardEventInit) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent(type, { bubbles: true, cancelable: true, ...init }));
  });
}

/** Removes `sessionId` from the cached list and the live sessions, as a delete or archive would. */
function removeSession(client: QueryClient, sessionId: string) {
  const live = new Set(["a", "b", "c"].filter((id) => id !== sessionId));
  act(() => {
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => live });
    client.setQueryData(MINE_SCOPE_KEY, (data: { pages: { data: { id: string }[] }[] }) => ({
      ...data,
      pages: data.pages.map((page) => ({
        ...page,
        data: page.data.filter((row) => row.id !== sessionId),
      })),
    }));
  });
}

const dialog = () => screen.getByRole("dialog", { name: "Switch to recent session" });
const activeOption = () =>
  within(dialog())
    .getAllByRole("option")
    .find((option) => option.getAttribute("aria-selected") === "true");
const listInput = () => within(dialog()).getByRole("combobox");

beforeEach(() => {
  navigate.mockClear();
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: ["a", "b", "c"],
    activeSessionId: "a",
    pendingSessionId: null,
  });
  useRecentSessionSwitchStore.setState({
    history: ["a", "b", "c"],
    readLiveSessionIds: () => new Set(["a", "b", "c"]),
    sessionsKnown: true,
    switcher: null,
    chordOwner: null,
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

it("lists the sessions by recency, marks the open one, and follows each press", async () => {
  renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  expect(
    within(dialog())
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual([
    expect.stringContaining("Session a"),
    expect.stringContaining("Session b"),
    expect.stringContaining("Session c"),
  ]);
  expect(within(dialog()).getAllByRole("option")[0]).toHaveTextContent("Current");
  expect(activeOption()).toHaveTextContent("Session b");
  expect(within(dialog()).getByText("Release ⌃ to switch")).toBeVisible();
  // The switcher has no query to type; its field only carries the list's keys.
  expect(listInput()).toHaveAttribute("readonly");

  act(() => stepRecentSessionSwitcher(1));
  expect(activeOption()).toHaveTextContent("Session c");
  act(() => stepRecentSessionSwitcher(1));
  expect(activeOption()).toHaveTextContent("Session a");
});

it("switches to the row Enter or a click picks, and the arrow keys move the selection", async () => {
  renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: false, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  fireEvent.keyDown(listInput(), { key: "ArrowDown" });
  expect(useRecentSessionSwitchStore.getState().switcher?.selectedId).toBe("c");
  fireEvent.keyDown(listInput(), { key: "Enter" });
  expect(navigate.mock.calls).toEqual([["/c/c"]]);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

  act(() => {
    openRecentSessionSwitcher(1, { holding: false, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  fireEvent.click(within(dialog()).getByRole("option", { name: /Session b/ }));
  expect(navigate.mock.calls.at(-1)).toEqual(["/c/b"]);
});

it("Esc closes it without switching", async () => {
  renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  fireEvent.keyDown(listInput(), { key: "Escape", ctrlKey: true });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(useRecentSessionSwitchStore.getState().switcher).toBeNull();
  expect(navigate).not.toHaveBeenCalled();
});

it("says it's loading until the sessions are known", async () => {
  useRecentSessionSwitchStore.setState({ sessionsKnown: false });
  renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  // Shown in the list, and announced.
  expect(within(dialog()).getAllByText("Loading sessions…")[0]).toBeVisible();
});

it.each([
  { chord: "⌃Tab", shiftKey: false, landsOn: "/c/b" },
  { chord: "⌃⇧Tab", shiftKey: true, landsOn: "/c/c" },
])(
  "$chord while the switcher is held switches by position, and the release adds nothing",
  async ({ shiftKey, landsOn }) => {
    // History a, c, b: the recency switcher's first step is c; position from a is b, or c going back.
    useRecentSessionSwitchStore.setState({ history: ["a", "c", "b"] });
    renderSwitcher();
    key("keydown", { key: "]", code: "BracketRight", ctrlKey: true });
    await waitFor(() => expect(dialog()).toBeVisible());
    key("keydown", { key: "Tab", code: "Tab", ctrlKey: true, shiftKey });
    expect(pathname).toBe(landsOn);
    expect(useRecentSessionSwitchStore.getState().switcher).toBeNull();

    key("keyup", { key: "Control" });
    expect(pathname).toBe(landsOn);
    expect(navigate).not.toHaveBeenCalled();
  },
);

it("moves on when the selected session is deleted, without reordering the rest", async () => {
  const client = renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
  });
  await waitFor(() => expect(activeOption()).toHaveTextContent("Session b"));
  removeSession(client, "b");
  await waitFor(() => expect(activeOption()).toHaveTextContent("Session c"));
  expect(
    within(dialog())
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual([expect.stringContaining("Session a"), expect.stringContaining("Session c")]);
  key("keyup", { key: "Control" });
  expect(navigate.mock.calls).toEqual([["/c/c"]]);
});

it.each(["release", "enter", "click"] as const)(
  "never opens a session that went away, on %s",
  async (accept) => {
    renderSwitcher();
    // Enter and a click accept a switcher opened from ⌘K; while held, any key releases it.
    act(() => {
      openRecentSessionSwitcher(1, { holding: accept === "release", navigate });
    });
    await waitFor(() => expect(activeOption()).toHaveTextContent("Session b"));
    // Gone before the switcher's list caught up, so its row still shows.
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => new Set(["a", "c"]) });
    if (accept === "release") key("keyup", { key: "Control" });
    else if (accept === "enter") fireEvent.keyDown(listInput(), { key: "Enter" });
    else fireEvent.click(activeOption()!);
    expect(navigate.mock.calls).toEqual(accept === "release" ? [["/c/c"]] : []);
  },
);

it("closes without switching once no other session is left", async () => {
  const client = renderSwitcher();
  act(() => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
  });
  await waitFor(() => expect(dialog()).toBeVisible());
  removeSession(client, "b");
  removeSession(client, "c");
  act(() => {
    useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => new Set() });
    client.setQueryData(MINE_SCOPE_KEY, (data: object) => ({ ...data }));
  });
  await waitFor(() => expect(useRecentSessionSwitchStore.getState().switcher).toBeNull());
  key("keyup", { key: "Control" });
  expect(navigate).not.toHaveBeenCalled();
});
