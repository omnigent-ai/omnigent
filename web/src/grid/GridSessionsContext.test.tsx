import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { toast } from "sonner";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { gridSessionIds, splitGrid } from "./gridLayout";
import { GridSessionsProvider, useGridSessions } from "./GridSessionsContext";

const identity = vi.hoisted(() => ({ viewer: "alice", server: "one" }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity.viewer }));
vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: () => identity.server }));

function Probe() {
  const grids = useGridSessions();
  const location = useLocation();
  return (
    <>
      <output data-testid="path">{location.pathname}</output>
      <output data-testid="order">
        {grids?.activeGrid && gridSessionIds(grids?.activeGrid.layout).join(",")}
      </output>
      <button type="button" onClick={() => grids?.swapPanes("first", "second")}>
        Swap
      </button>
      <output data-testid="name">{grids?.activeGrid?.name}</output>
      <button type="button" onClick={() => grids?.renameGrid(grids?.activeGrid!.id, "  My work  ")}>
        Rename
      </button>
      <button type="button" onClick={() => grids?.split("first", "third", "bottom")}>
        Add third
      </button>
      <button type="button" onClick={() => grids?.closePane("third")}>
        Close third
      </button>
      <output data-testid="count">{grids?.grids.length ?? 0}</output>
      <button type="button" onClick={() => grids?.split("first", "second", "right")}>
        Split
      </button>
      <button type="button" onClick={() => grids?.closePane("second")}>
        Close
      </button>
    </>
  );
}

function tree(path: string, enabled = true) {
  const element = (
    <GridSessionsProvider>
      <Probe />
    </GridSessionsProvider>
  );
  return (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { grid_sessions_display_ui: enabled } }}
    >
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/c/:conversationId" element={element} />
          <Route path="/grid/:gridId" element={element} />
        </Routes>
      </MemoryRouter>
    </CapabilitiesProvider>
  );
}

beforeEach(() => {
  identity.viewer = "alice";
  identity.server = "one";
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each([true, false])("persists and scopes grids (secure context: %s)", (secureContext) => {
  if (!secureContext)
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
  const view = render(tree("/c/first"));
  fireEvent.click(screen.getByText("Split"));
  const path = screen.getByTestId("path").textContent!;
  expect(path).toMatch(/^\/grid\//);
  view.unmount();
  const restored = render(tree(path));
  expect(screen.getByTestId("count")).toHaveTextContent("1");
  identity.viewer = "bob";
  restored.rerender(tree(path));
  expect(screen.getByTestId("count")).toHaveTextContent("0");
  identity.viewer = "alice";
  identity.server = "two";
  restored.rerender(tree(path));
  expect(screen.getByTestId("count")).toHaveTextContent("0");
  identity.server = "one";
  restored.rerender(tree(path));
  expect(screen.getByTestId("count")).toHaveTextContent("1");
  fireEvent.click(screen.getByText("Close"));
  expect(screen.getByTestId("path")).toHaveTextContent("/c/first");
  expect(screen.getByTestId("count")).toHaveTextContent("0");
});

it("gates creation and keeps an unsaved grid usable when storage is full", () => {
  const read = vi.spyOn(Storage.prototype, "getItem");
  const view = render(tree("/c/first", false));
  fireEvent(
    window,
    new StorageEvent("storage", {
      key: "omnigent:session-grids:one:alice",
      storageArea: localStorage,
    }),
  );
  expect(read).not.toHaveBeenCalledWith("omnigent:session-grids:one:alice");
  fireEvent.click(screen.getByText("Split"));
  expect(screen.getByTestId("path")).toHaveTextContent("/c/first");
  expect(localStorage.length).toBe(0);
  view.rerender(tree("/c/first"));
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  const warning = vi.spyOn(toast, "warning").mockReturnValue("warning");
  fireEvent.click(screen.getByText("Split"));
  expect(screen.getByTestId("path").textContent).toMatch(/^\/grid\//);
  expect(screen.getByTestId("count")).toHaveTextContent("1");
  expect(warning).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText("Rename"));
  fireEvent.click(screen.getByText("Add third"));
  expect(screen.getByTestId("name")).toHaveTextContent("My work");
  expect(screen.getByTestId("order")).toHaveTextContent("first,third,second");
});

it("syncs other tabs' grids before editing and observes removals", () => {
  const key = "omnigent:session-grids:one:alice";
  const layout = splitGrid("first", "first", "second", "right")!;
  const grid = { id: "shared", layout };
  localStorage.setItem(key, JSON.stringify([grid]));
  render(tree("/grid/shared"));
  const other = { id: "other", name: "Other work", layout };
  const latest = [{ ...grid, name: "Renamed in another tab" }, other];
  localStorage.setItem(key, JSON.stringify(latest));
  fireEvent(window, new StorageEvent("storage", { key, storageArea: sessionStorage }));
  fireEvent(
    window,
    new StorageEvent("storage", { key: `${key}-other`, storageArea: localStorage }),
  );
  expect(screen.getByTestId("count")).toHaveTextContent("1");
  fireEvent(window, new StorageEvent("storage", { key, storageArea: localStorage }));
  expect(screen.getByTestId("count")).toHaveTextContent("2");
  expect(screen.getByTestId("name")).toHaveTextContent("Renamed in another tab");
  fireEvent.click(screen.getByText("Add third"));
  expect(JSON.parse(localStorage.getItem(key)!)).toEqual([
    { ...latest[0], layout: splitGrid(layout, "first", "third", "bottom") },
    other,
  ]);
  localStorage.setItem(key, JSON.stringify([other]));
  fireEvent(window, new StorageEvent("storage", { key, storageArea: localStorage }));
  fireEvent.click(screen.getByText("Add third"));
  expect(screen.getByTestId("order")).toBeEmptyDOMElement();
  expect(JSON.parse(localStorage.getItem(key)!)).toEqual([other]);
  localStorage.clear();
  fireEvent(window, new StorageEvent("storage", { key: null, storageArea: localStorage }));
  expect(screen.getByTestId("count")).toHaveTextContent("0");
});

it("keeps a trimmed custom name through layout changes and reloads", () => {
  const view = render(tree("/c/first"));
  fireEvent.click(screen.getByText("Split"));
  fireEvent.click(screen.getByText("Rename"));
  fireEvent.click(screen.getByText("Add third"));
  expect(screen.getByTestId("name")).toHaveTextContent("My work");
  fireEvent.click(screen.getByText("Close third"));
  fireEvent.click(screen.getByText("Swap"));
  expect(screen.getByTestId("order")).toHaveTextContent("second,first");
  const path = screen.getByTestId("path").textContent!;
  view.unmount();
  render(tree(path));
  expect(screen.getByTestId("name")).toHaveTextContent("My work");
  expect(screen.getByTestId("order")).toHaveTextContent("second,first");
});
