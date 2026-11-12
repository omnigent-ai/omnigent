import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";

// Behavior specific to the QuickPick palette; CommandPalette.quickPick.test.tsx runs
// every shared CommandPalette case against it.

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
const useConversations = vi.fn();
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: () => ({ isFetching: false }),
}));

beforeEach(() => {
  navigate.mockClear();
  useConversations.mockReturnValue({
    data: {
      pages: [{ data: [{ id: "c1", title: "Fix the parser", agent_name: null, archived: false }] }],
    },
    isFetching: false,
  });
});
afterEach(cleanup);

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

describe("QuickPickCommandPalette", () => {
  it("lines session labels up with action labels through a blank icon slot", () => {
    renderPalette();
    const session = screen.getByRole("option", { name: /Fix the parser/ });
    const action = screen.getByRole("option", { name: /Go to Settings/ });
    // Both rows start with the same icon slot; the session's is empty.
    const sessionSlot = session.firstElementChild;
    const actionSlot = action.firstElementChild;
    expect(sessionSlot?.className).toBe(actionSlot?.className);
    expect(sessionSlot?.querySelector("svg")).toBeNull();
    expect(actionSlot?.querySelector("svg")).toBeTruthy();
  });

  it("finds actions by their initials and highlights the matched letters", () => {
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "gts" } });
    const actions = within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option");
    expect(actions[0].textContent).toContain("Go to Settings");
    expect(actions[0].querySelectorAll("mark").length).toBeGreaterThan(0);
  });

  it("moves between groups with Alt+Arrow and runs the action with Enter", () => {
    const { input, onOpenChange } = renderPalette();
    fireEvent.keyDown(input, { key: "ArrowDown", altKey: true });
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active?.closest('[role="group"]')?.getAttribute("aria-labelledby")).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option")[0]).toBe(
      active,
    );
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
