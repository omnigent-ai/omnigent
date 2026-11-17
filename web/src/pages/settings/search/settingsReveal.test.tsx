// Revealing a Settings row from its URL hash: it scrolls into view, takes focus on its control
// and is marked briefly. A navigation starts a new reveal window, and a row that mounts after
// the window doesn't move the page.

import { act, cleanup, render, screen } from "@testing-library/react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseSettingsHash } from "./settingsControl";
import { settingsEntry, type SettingsEntry } from "./catalog/types";
import {
  noteSettingsNavigation,
  registerSettingsAnchor,
  revealSettingsRow,
  SETTINGS_HIGHLIGHT_ATTRIBUTE,
  SETTINGS_HIGHLIGHT_MS,
  SETTINGS_REVEAL_WINDOW_MS,
  SETTINGS_REVEALER_GRACE_MS,
} from "./settingsReveal";

const fixtureEntries = vi.hoisted(() => new Map<string, unknown>());
vi.mock("./catalog", () => ({
  settingsEntryById: (id: string) => fixtureEntries.get(id),
}));

const ROW = settingsEntry({
  id: "fixture-row",
  section: "general",
  label: "Fixture row",
  description: "A row for the reveal tests.",
  parts: { field: "Field", reset: "Reset" },
});

// Revealed through the row's own main control when it isn't on the page.
const CHILD = settingsEntry({
  id: "fixture-child",
  section: "general",
  label: "Fixture child",
  description: "A row that the fixture row reveals while it is hidden.",
  revealedBy: "fixture-row",
});

let navigate: (path: string) => void = () => {};

/** A row on the page, with its main control and two parts, as Settings renders one. */
function FixtureRow({ entry, mountAfter = 0 }: { entry: SettingsEntry; mountAfter?: number }) {
  const location = useLocation();
  const rowRef = useRef<HTMLElement>(null);
  const [mounted, setMounted] = useState(mountAfter === 0);
  useEffect(() => {
    if (mountAfter === 0) return;
    const timer = window.setTimeout(() => setMounted(true), mountAfter);
    return () => window.clearTimeout(timer);
  }, [mountAfter]);
  useEffect(() => {
    const target = parseSettingsHash(location.hash);
    if (!mounted || !target || !rowRef.current) return;
    const releaseAnchor = registerSettingsAnchor(entry.id);
    const controller = new AbortController();
    revealSettingsRow({ row: rowRef.current, entry, target, location, signal: controller.signal });
    return () => {
      controller.abort();
      releaseAnchor();
    };
  }, [entry, location, mounted]);
  if (!mounted) return null;
  return (
    <section data-settings-item={entry.id} ref={rowRef}>
      <button type="button" data-settings-control={`${entry.id}:`}>
        Toggle
      </button>
      <input data-settings-control={`${entry.id}:field`} aria-label="Field" />
      <button type="button" data-settings-control={`${entry.id}:reset`} aria-label="Reset">
        Reset
      </button>
    </section>
  );
}

function Page({ rows, children }: { rows: ReactNode; children?: ReactNode }) {
  const location = useLocation();
  const go = useNavigate();
  // The Settings page notes each navigation before its rows render.
  noteSettingsNavigation(location);
  useEffect(() => {
    navigate = (path) => go(path);
  }, [go]);
  return (
    <>
      {rows}
      {children}
    </>
  );
}

function renderAt(hash: string, rows: ReactNode) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/settings/general", hash }]}>
      <Page rows={rows} />
    </MemoryRouter>,
  );
}

const row = () => document.querySelector<HTMLElement>(`[data-settings-item="${ROW.id}"]`)!;
const highlighted = () => row().hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE);

let scrolled: Element[] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  scrolled = [];
  Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
    scrolled.push(this);
  };
  fixtureEntries.clear();
  fixtureEntries.set(CHILD.id, CHILD);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("revealing a Settings row from its hash", () => {
  it("scrolls to the row, focuses the named part and marks the row until the mark fades", () => {
    renderAt("#fixture-row:field", <FixtureRow entry={ROW} />);
    expect(scrolled).toEqual([row()]);
    expect(screen.getByRole("textbox", { name: "Field" })).toHaveFocus();
    expect(highlighted()).toBe(true);
    act(() => vi.advanceTimersByTime(SETTINGS_HIGHLIGHT_MS));
    expect(highlighted()).toBe(false);
  });

  it("focuses the row's main control for a bare row, which is its first control", () => {
    renderAt("#fixture-row", <FixtureRow entry={ROW} />);
    expect(screen.getByRole("button", { name: "Toggle" })).toHaveFocus();
  });

  it("names the main control with a trailing colon, apart from the bare row", () => {
    renderAt("#fixture-row:", <FixtureRow entry={ROW} />);
    expect(screen.getByRole("button", { name: "Toggle" })).toHaveFocus();
  });

  it("focuses the row's main control for a part it doesn't show", () => {
    renderAt("#fixture-row:missing", <FixtureRow entry={ROW} />);
    expect(screen.getByRole("button", { name: "Toggle" })).toHaveFocus();
  });

  it("reveals the same row again on each navigation to it", () => {
    renderAt("#fixture-row:field", <FixtureRow entry={ROW} />);
    act(() => vi.advanceTimersByTime(SETTINGS_HIGHLIGHT_MS));
    screen.getByRole("button", { name: "Reset" }).focus();
    act(() => navigate("/settings/general#fixture-row:field"));
    expect(scrolled).toHaveLength(2);
    expect(screen.getByRole("textbox", { name: "Field" })).toHaveFocus();
    expect(highlighted()).toBe(true);
  });

  it("drops the mark when a newer navigation goes to another hash", () => {
    renderAt("#fixture-row:field", <FixtureRow entry={ROW} />);
    expect(highlighted()).toBe(true);
    act(() => navigate("/settings/general#somewhere-else"));
    expect(highlighted()).toBe(false);
  });

  it("reveals a row that mounts shortly after the navigation", () => {
    renderAt("#fixture-row:field", <FixtureRow entry={ROW} mountAfter={500} />);
    expect(scrolled).toEqual([]);
    act(() => vi.advanceTimersByTime(500));
    expect(scrolled).toEqual([row()]);
    expect(screen.getByRole("textbox", { name: "Field" })).toHaveFocus();
  });

  it("leaves the page alone when the row mounts after the reveal window", () => {
    renderAt(
      "#fixture-row:field",
      <FixtureRow entry={ROW} mountAfter={SETTINGS_REVEAL_WINDOW_MS + 500} />,
    );
    act(() => vi.advanceTimersByTime(SETTINGS_REVEAL_WINDOW_MS + 500));
    expect(row()).toBeInTheDocument();
    expect(scrolled).toEqual([]);
    expect(highlighted()).toBe(false);
  });

  it("reveals a hidden row through the row that shows it, once the hidden row doesn't appear", () => {
    renderAt("#fixture-child", <FixtureRow entry={ROW} />);
    expect(scrolled).toEqual([]);
    act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
    expect(scrolled).toEqual([row()]);
    expect(screen.getByRole("button", { name: "Toggle" })).toHaveFocus();
  });

  it("doesn't reveal through a revealer once the hidden row has appeared", () => {
    renderAt(
      "#fixture-child",
      <>
        <FixtureRow entry={ROW} />
        <FixtureRow entry={CHILD} />
      </>,
    );
    act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
    expect(scrolled).toEqual([document.querySelector(`[data-settings-item="${CHILD.id}"]`)]);
    expect(highlighted()).toBe(false);
  });
});
