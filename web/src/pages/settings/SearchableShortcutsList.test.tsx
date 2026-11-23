import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as NativeBridge from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

import { SearchableShortcutsList } from "./SearchableShortcutsList";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ sibling_sessions: false, features: {} }),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => false,
  isElectronShell: () => false,
  supportsBrowser: () => false,
  supportsDesktopCommands: () => false,
}));

let navigate: (to: string) => void = () => {};
function Navigator() {
  const go = useNavigate();
  navigate = (to) => void go(to);
  return null;
}

function renderList() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/settings/shortcuts"]}>
        <Navigator />
        <SearchableShortcutsList />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.getByRole("searchbox", { name: "Search shortcuts" });
}

const rowNames = () => screen.getAllByRole("listitem").map((row) => row.firstChild?.textContent);

beforeEach(() => {
  Object.defineProperty(navigator, "platform", { value: "MacIntel", configurable: true });
});
afterEach(cleanup);

describe("SearchableShortcutsList", () => {
  it("filters the rows as you type, and clears with Escape", async () => {
    const field = renderList();
    const all = rowNames().length;
    await userEvent.type(field, "model");
    expect(rowNames()).toEqual(["Open model picker", "Cycle model"]);
    expect(screen.getByRole("heading", { name: "In chats" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "General" })).toBeNull();

    await userEvent.keyboard("{Escape}");
    expect(field).toHaveValue("");
    expect(rowNames()).toHaveLength(all);
  });

  it("says when nothing matches", async () => {
    const field = renderList();
    await userEvent.type(field, "no such shortcut");
    expect(screen.getByRole("status")).toHaveTextContent("No shortcuts match “no such shortcut”");
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("shows every row again when a link names one, so it can be revealed", async () => {
    const field = renderList();
    await userEvent.type(field, "model");
    act(() => navigate("/settings/shortcuts#shortcut.palette.commands"));
    expect(field).toHaveValue("");
    expect(
      document.querySelector('[data-settings-item="shortcut.palette.commands"]'),
    ).not.toBeNull();
  });

  it("finds a row by the chord typed into the field", () => {
    const field = renderList();
    fireEvent.change(field, { target: { value: "⌘K" } });
    expect(rowNames()).toEqual(["Open command palette"]);
  });
});
