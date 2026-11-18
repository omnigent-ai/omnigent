// A part search finds but the page doesn't show yet, such as a control inside a closed dialog
// or a field of a form that isn't open, is revealed through its opener. A part shown only in
// some states is revealed in place, or, while it isn't usable, through the row's other controls.
// These search the index and open the page at the resolved path directly; choosing in the search
// box itself is SettingsSearch.openers.test.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsEntry } from "./catalog/types";
import { SETTINGS_MAIN_CONTROL, settingsEntry } from "./catalog/types";
import { SettingsItem, useSettingsNavigationClock } from "./SettingsItem";
import { settingsControlProps, settingsItemPath, settingsRevealPart } from "./settingsControl";
import { SETTINGS_HIGHLIGHT_ATTRIBUTE, SETTINGS_REVEAL_WINDOW_MS } from "./settingsReveal";

/** Settings rows read shell support through queries, as under the app. */
const queryClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

/** A row whose Mark as fixed and Undo buttons sit in the dialog its How to fix… button opens. */
const SHORTCUT = settingsEntry({
  id: "fixture.shortcut",
  section: "general",
  label: "Open from anywhere",
  description: "A keyboard shortcut that works in every app.",
  parts: { fix: "How to fix…", done: "Mark as fixed", undo: "Undo the fix" },
  presence: { done: { overlay: "fix" }, undo: { overlay: "fix" } },
});
/** A row whose Agent name field is in the form its main control opens; Import is its focus. */
const AGENTS = settingsEntry({
  id: "fixture.agents",
  section: "custom-agents",
  label: "Create custom agent",
  description: "Make an agent of your own.",
  parts: { import: "Import agent", name: "Agent name" },
  focus: "import",
  presence: { name: { states: ["new agent form"] } },
  reachedBy: { name: SETTINGS_MAIN_CONTROL },
});
/** Inline actions of one row, each shown only in some states. */
const TITLES = settingsEntry({
  id: "fixture.titles",
  section: "general",
  label: "Title instructions",
  description: "How sessions are named.",
  parts: { save: "Save titles", retry: "Retry titles" },
  presence: { save: { states: ["loaded"] }, retry: { states: ["load failed"] } },
});
const ENTRIES: SettingsEntry[] = [SHORTCUT, AGENTS, TITLES];

vi.mock("./catalog", () => ({
  settingsEntryById: (id: string) => ENTRIES.find((entry) => entry.id === id),
}));

function Clock({ children }: { children: ReactNode }) {
  useSettingsNavigationClock();
  return children;
}

const openFix = vi.fn();
const openForm = vi.fn();

/** The shortcut and agents rows as the page shows them with every dialog and form closed. */
function Rows() {
  return (
    <>
      <SettingsItem entry={SHORTCUT}>
        {/* The row's own switch comes first, so the opener isn't merely its first control. */}
        <button
          type="button"
          role="switch"
          aria-checked="true"
          {...settingsControlProps(SHORTCUT)}
        />
        <button type="button" onClick={openFix} {...settingsControlProps(SHORTCUT, "fix")}>
          How to fix…
        </button>
      </SettingsItem>
      <SettingsItem entry={AGENTS}>
        <button type="button" {...settingsControlProps(AGENTS, "import")}>
          Import agent
        </button>
        <button type="button" onClick={openForm} {...settingsControlProps(AGENTS)}>
          New agent
        </button>
      </SettingsItem>
    </>
  );
}

/** The titles row in one state: loaded (Save, disabled until an edit) or failed (Retry). */
function Titles({
  state,
  saveEnabled = false,
}: {
  state: "loaded" | "load failed";
  saveEnabled?: boolean;
}) {
  return (
    <SettingsItem entry={TITLES}>
      {/* The row's help button and a nested row's control must never take the reveal. */}
      <button type="button" aria-label="About Title instructions">
        ?
      </button>
      <textarea disabled={state === "load failed"} {...settingsControlProps(TITLES)} />
      {state === "loaded" && (
        <button type="button" disabled={!saveEnabled} {...settingsControlProps(TITLES, "save")}>
          Save
        </button>
      )}
      {state === "load failed" && (
        <button type="button" {...settingsControlProps(TITLES, "retry")}>
          Retry
        </button>
      )}
    </SettingsItem>
  );
}

let navigation = 0;
/** Opens the page where choosing `part` of `entry` leads, as a search result would. */
function chooseResult(entry: SettingsEntry, part: string | undefined, rows: ReactNode = <Rows />) {
  const path = settingsItemPath(entry, settingsRevealPart(entry, part));
  const [pathname, hash] = path.split("#");
  navigation += 1;
  render(
    <QueryClientProvider client={queryClient()}>
      <MemoryRouter initialEntries={[{ pathname, hash: `#${hash}`, key: `opener-${navigation}` }]}>
        <Clock>{rows}</Clock>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { path };
}

const row = (entry: SettingsEntry) =>
  document.querySelector<HTMLElement>(`[data-settings-item="${entry.id}"]`)!;
const control = (value: string) =>
  document.querySelector<HTMLElement>(`[data-settings-control="${value}"]`);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  Element.prototype.scrollIntoView = vi.fn();
  openFix.mockClear();
  openForm.mockClear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("a part only a dialog shows", () => {
  it("is revealed through the button that opens the dialog, which isn't pressed", () => {
    const { path } = chooseResult(SHORTCUT, "done");
    expect(path).toBe("/settings/general#fixture.shortcut:fix");
    expect(row(SHORTCUT).scrollIntoView).toHaveBeenCalled();
    expect(row(SHORTCUT).hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE)).toBe(true);
    expect(row(AGENTS).hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE)).toBe(false);
    expect(document.activeElement).toBe(control("fixture.shortcut:fix"));
    expect(openFix).not.toHaveBeenCalled();
  });

  it("reveals a part and the sibling sharing its opener through the same path", () => {
    const revealed = (part: string) =>
      settingsItemPath(SHORTCUT, settingsRevealPart(SHORTCUT, part));
    expect(revealed("undo")).toBe(revealed("done"));
  });
});

describe("a field only a form shows", () => {
  it("is revealed through the main control that opens the form, not the row's usual focus", () => {
    const { path } = chooseResult(AGENTS, "name");
    expect(path).toBe("/settings/custom-agents#fixture.agents:");
    expect(row(AGENTS).hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE)).toBe(true);
    expect(document.activeElement).toBe(control("fixture.agents:"));
    expect(openForm).not.toHaveBeenCalled();
  });

  it("waits for the main control while it's hidden, then focuses it", async () => {
    for (const hiding of ["hidden", "aria-hidden", "inert"]) {
      chooseResult(
        AGENTS,
        "name",
        <SettingsItem entry={AGENTS}>
          <button type="button" {...settingsControlProps(AGENTS, "import")}>
            Import agent
          </button>
          <div
            // Set before the reveal runs: React doesn't render every one of these from props.
            ref={(wrapper) => wrapper?.setAttribute(hiding, hiding === "aria-hidden" ? "true" : "")}
            data-testid="wrapper"
          >
            <button type="button" {...settingsControlProps(AGENTS)}>
              New agent
            </button>
          </div>
        </SettingsItem>,
      );
      // The main control is what was asked for, so the reveal doesn't settle for Import.
      expect(document.activeElement).toBe(document.body);
      document.querySelector("[data-testid=wrapper]")!.removeAttribute(hiding);
      // oxlint-disable-next-line no-await-in-loop -- each hiding attribute in turn
      await vi.waitFor(() => expect(document.activeElement).toBe(control("fixture.agents:")));
      cleanup();
    }
  });

  it("waits for an ancestor outside the row to stop hiding it", async () => {
    for (const hiding of ["hidden", "aria-hidden", "inert"]) {
      chooseResult(
        AGENTS,
        "name",
        <div
          ref={(wrapper) => wrapper?.setAttribute(hiding, hiding === "aria-hidden" ? "true" : "")}
          data-testid="drawer"
        >
          <Rows />
        </div>,
      );
      expect(document.activeElement).toBe(document.body);
      document.querySelector("[data-testid=drawer]")!.removeAttribute(hiding);
      // oxlint-disable-next-line no-await-in-loop -- each hiding attribute in turn
      await vi.waitFor(() => expect(document.activeElement).toBe(control("fixture.agents:")));
      cleanup();
    }
  });

  it("leaves the row's own reveal on its usual focus", () => {
    chooseResult(AGENTS, undefined);
    expect(document.activeElement).toBe(control("fixture.agents:import"));
  });
});

describe("a part shown only in some states", () => {
  it("is revealed in place while it's usable", () => {
    chooseResult(TITLES, "retry", <Titles state="load failed" />);
    expect(document.activeElement).toBe(control("fixture.titles:retry"));
    cleanup();
    chooseResult(TITLES, "save", <Titles state="loaded" saveEnabled />);
    expect(document.activeElement).toBe(control("fixture.titles:save"));
  });

  it("lands on the row's main control while it's disabled or absent", () => {
    chooseResult(TITLES, "save", <Titles state="loaded" />);
    expect(document.activeElement).toBe(control("fixture.titles:"));
    cleanup();
    chooseResult(TITLES, "retry", <Titles state="loaded" />);
    expect(document.activeElement).toBe(control("fixture.titles:"));
  });

  it("lands on another of the row's controls when the main one can't take focus", () => {
    // A failed load leaves the field disabled and Save gone; Retry is what's usable.
    chooseResult(TITLES, "save", <Titles state="load failed" />);
    expect(document.activeElement).toBe(control("fixture.titles:retry"));
    expect(document.activeElement).not.toHaveAccessibleName("About Title instructions");
  });

  it("takes focus over from its stand-in once it appears with the row's data", async () => {
    // As Select on Archived sessions, which shows only once the archived list arrives.
    let showRetry: () => void = () => {};
    function LateRetry() {
      const [failed, setFailed] = useState(false);
      showRetry = () => setFailed(true);
      return <Titles state={failed ? "load failed" : "loaded"} />;
    }
    chooseResult(TITLES, "retry", <LateRetry />);
    expect(document.activeElement).toBe(control("fixture.titles:"));
    act(() => showRetry());
    await vi.waitFor(() => expect(document.activeElement).toBe(control("fixture.titles:retry")));
  });

  it("keeps a stand-in the user has started using", async () => {
    let enableSave: () => void = () => {};
    function LateSave() {
      const [enabled, setEnabled] = useState(false);
      enableSave = () => setEnabled(true);
      return <Titles state="loaded" saveEnabled={enabled} />;
    }
    for (const input of ["keydown", "pointerdown"] as const) {
      chooseResult(TITLES, "save", <LateSave />);
      const field = control("fixture.titles:")!;
      expect(document.activeElement).toBe(field);
      // Typing in the field is what enables Save; the user's caret stays where it is.
      if (input === "keydown") fireEvent.keyDown(field, { key: "a" });
      else fireEvent.pointerDown(field);
      act(() => enableSave());
      // oxlint-disable-next-line no-await-in-loop -- each kind of input in turn
      await act(async () => {});
      expect(document.activeElement).toBe(field);
      cleanup();
    }
  });

  it.each([
    ["types", (field: HTMLElement) => fireEvent.keyDown(field, { key: "a" })],
    ["clicks", (field: HTMLElement) => fireEvent.pointerDown(field)],
  ])(
    "stops waiting once the user %s in another field of the row, before any control has focus",
    async (_verb, interact) => {
      let enableSave: () => void = () => {};
      function LateSave() {
        const [enabled, setEnabled] = useState(false);
        enableSave = () => setEnabled(true);
        return (
          <SettingsItem entry={TITLES}>
            <textarea disabled {...settingsControlProps(TITLES)} />
            <input aria-label="Notes" />
            {enabled && (
              <button type="button" {...settingsControlProps(TITLES, "save")}>
                Save
              </button>
            )}
          </SettingsItem>
        );
      }
      chooseResult(TITLES, "save", <LateSave />);
      const notes = document.querySelector<HTMLInputElement>('input[aria-label="Notes"]')!;
      notes.focus();
      interact(notes);
      act(() => enableSave());
      await act(async () => {});
      expect(document.activeElement).toBe(notes);
    },
  );

  it("stops waiting to take over once the reveal window closes", async () => {
    let enableSave: () => void = () => {};
    function LateSave() {
      const [enabled, setEnabled] = useState(false);
      enableSave = () => setEnabled(true);
      return <Titles state="loaded" saveEnabled={enabled} />;
    }
    chooseResult(TITLES, "save", <LateSave />);
    act(() => vi.advanceTimersByTime(SETTINGS_REVEAL_WINDOW_MS + 1));
    act(() => enableSave());
    await act(async () => {});
    expect(document.activeElement).toBe(control("fixture.titles:"));
  });

  it("never lands on another row's control nested inside it", async () => {
    chooseResult(
      TITLES,
      "save",
      <SettingsItem entry={TITLES}>
        <textarea disabled {...settingsControlProps(TITLES)} />
        <SettingsItem entry={AGENTS}>
          <button type="button" {...settingsControlProps(AGENTS)}>
            New agent
          </button>
        </SettingsItem>
      </SettingsItem>,
    );
    expect(document.activeElement).toBe(document.body);
    // Once the field is usable, it takes the focus the reveal was waiting to give.
    const field = control("fixture.titles:") as HTMLTextAreaElement;
    field.disabled = false;
    await vi.waitFor(() => expect(document.activeElement).toBe(field));
  });
});
