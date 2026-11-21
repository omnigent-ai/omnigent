// ⌘↵ on a session row in the QuickPick palette: that session's actions on a page of their own.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";
import { OWNER_ONLY_REASONS } from "./sessionCommands/model";
import { useSessionActionStore } from "./sessionCommands/sessionActionStore";

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
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: () => ({ isFetching: false }),
  useProjects: () => ({ data: [] }),
}));

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

function renderPalette() {
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuickPickCommandPalette
        open
        onOpenChange={onOpenChange}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
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
    expect(useSessionActionStore.getState().request).toMatchObject({
      command: "mark-unread",
      conversation: { id: "c1" },
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("hands an action that opens a dialog to the host only once the palette has closed", async () => {
    const { input, onOpenChange } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).not.toBeNull());
    fireEvent.change(input, { target: { value: "rename" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    // The palette is still open in this test, so its close hasn't finished yet.
    expect(useSessionActionStore.getState().request).toBeNull();
  });

  it("keeps the page on a greyed action, asking nothing of the host", async () => {
    listSessions(SHARED, MINE);
    const { input, onOpenChange } = renderPalette();
    openActions(input);
    await waitFor(() => expect(heading()).not.toBeNull());
    fireEvent.change(input, { target: { value: "rename" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(useSessionActionStore.getState().request).toBeNull();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(heading()).toBe("Actions for “Review notes”");
  });

  it("opens the session as before on a plain Enter", () => {
    const { input } = renderPalette();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c1");
  });
});
