// Revealing a Settings row from its URL hash: it scrolls into view, takes focus on its control
// and is marked briefly. A navigation starts a new reveal window, and a row that mounts after
// the window doesn't move the page.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

/** The rows a reveal centered, apart from the controls it then brought into view. */
let scrolled: Element[] = [];
let scrollOptions: (ScrollIntoViewOptions | boolean | undefined)[] = [];
let controlsScrolled: Element[] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  scrolled = [];
  scrollOptions = [];
  controlsScrolled = [];
  Element.prototype.scrollIntoView = function scrollIntoView(
    this: Element,
    options?: ScrollIntoViewOptions | boolean,
  ) {
    scrollOptions.push(options);
    if (typeof options === "object" && options.block === "nearest") controlsScrolled.push(this);
    else scrolled.push(this);
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
    const field = screen.getByRole("textbox", { name: "Field" });
    expect(scrolled).toEqual([row()]);
    expect(field).toHaveFocus();
    expect(highlighted()).toBe(true);
    act(() => vi.advanceTimersByTime(SETTINGS_HIGHLIGHT_MS));
    expect(highlighted()).toBe(false);
  });

  it("centers the row, then brings the focused control into view, as a tall row can hide it", () => {
    renderAt("#fixture-row:reset", <FixtureRow entry={ROW} />);
    const reset = screen.getByRole("button", { name: "Reset" });
    expect(reset).toHaveFocus();
    expect(scrolled).toEqual([row()]);
    expect(controlsScrolled).toEqual([reset]);
    expect(scrollOptions).toEqual([{ block: "center" }, { block: "nearest" }]);
  });

  /** Runs `body` with a ResizeObserver that `resize` drives and whose live observers it counts. */
  function withResizeObserver(body: (resize: () => void, live: () => number) => void) {
    const observers = new Set<() => void>();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        private readonly callback: () => void;
        constructor(callback: () => void) {
          this.callback = callback;
        }
        observe() {
          observers.add(this.callback);
        }
        disconnect() {
          observers.delete(this.callback);
        }
      },
    );
    try {
      body(
        () => [...observers].forEach((callback) => callback()),
        () => observers.size,
      );
    } finally {
      vi.unstubAllGlobals();
    }
  }

  it("keeps the focused control in view while its row resizes, until the user acts", () => {
    withResizeObserver((resize, live) => {
      renderAt("#fixture-row:reset", <FixtureRow entry={ROW} />);
      const reset = screen.getByRole("button", { name: "Reset" });
      expect(controlsScrolled).toEqual([reset]);
      // Content loading above it, such as a prompt editor, pushes it down.
      act(resize);
      expect(controlsScrolled).toEqual([reset, reset]);
      fireEvent.keyDown(document, { key: "ArrowDown" });
      expect(live()).toBe(0);
      act(resize);
      expect(controlsScrolled).toEqual([reset, reset]);
    });
  });

  it("stops keeping the control in view once the reveal window closes", () => {
    withResizeObserver((resize, live) => {
      renderAt("#fixture-row:reset", <FixtureRow entry={ROW} />);
      const reset = screen.getByRole("button", { name: "Reset" });
      expect(live()).toBe(1);
      act(() => vi.advanceTimersByTime(SETTINGS_REVEAL_WINDOW_MS));
      expect(live()).toBe(0);
      act(resize);
      expect(controlsScrolled).toEqual([reset]);
    });
  });

  it("stops keeping the control in view when the user navigates away", () => {
    withResizeObserver((resize, live) => {
      renderAt("#fixture-row:reset", <FixtureRow entry={ROW} />);
      const reset = screen.getByRole("button", { name: "Reset" });
      act(() => navigate("/settings/general#somewhere-else"));
      expect(live()).toBe(0);
      act(resize);
      expect(controlsScrolled).toEqual([reset]);
    });
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
