// A row hidden by the current state, such as the custom theme editor while a preset is on,
// is revealed through the visible row that shows it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { type ReactNode, useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { SettingsEntry } from "./catalog/types";
import { SettingsItem, useSettingsNavigationClock } from "./SettingsItem";
import {
  SETTINGS_HIGHLIGHT_ATTRIBUTE,
  SETTINGS_REVEAL_WINDOW_MS,
  SETTINGS_REVEALER_GRACE_MS,
} from "./settingsReveal";

/** Settings rows read shell support through queries, as under the app. */
const queryClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

const REVEALER: SettingsEntry = {
  id: "fixture.theme",
  section: "appearance",
  label: "Color theme",
  description: "",
};
const HIDDEN: SettingsEntry = {
  id: "fixture.custom-theme",
  section: "appearance",
  label: "Custom theme",
  description: "",
  revealedBy: REVEALER.id,
};

vi.mock("./catalog", () => ({
  settingsEntryById: (id: string) => [REVEALER, HIDDEN].find((entry) => entry.id === id),
}));

function Clock({ children }: { children: ReactNode }) {
  useSettingsNavigationClock();
  return children;
}

function MountLater({ entry, after }: { entry: SettingsEntry; after: number }) {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setShown(true), after);
    return () => clearTimeout(timer);
  }, [after]);
  return shown ? <SettingsItem entry={entry}>{entry.label}</SettingsItem> : null;
}

let testKey = 0;
function renderRows(entries: SettingsEntry[], later: ReactNode = null) {
  testKey += 1;
  return render(
    <QueryClientProvider client={queryClient()}>
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/settings/appearance",
            hash: `#${HIDDEN.id}`,
            key: `revealed-by-${testKey}`,
          },
        ]}
      >
        <Clock>
          {entries.map((entry) => (
            <SettingsItem key={entry.id} entry={entry}>
              {entry.label}
            </SettingsItem>
          ))}
          {later}
        </Clock>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const marked = (entry: SettingsEntry) =>
  document
    .querySelector(`[data-settings-item="${entry.id}"]`)
    ?.hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE) ?? false;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("reveals the revealer once the hidden row hasn't appeared", () => {
  renderRows([REVEALER]);
  expect(marked(REVEALER)).toBe(false);
  act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
  expect(marked(REVEALER)).toBe(true);
});

it("reveals the row itself when it is on screen", () => {
  renderRows([REVEALER, HIDDEN]);
  expect(marked(HIDDEN)).toBe(true);
  act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
  expect(marked(REVEALER)).toBe(false);
});

it("hands the mark to the row once it appears after its revealer took over", () => {
  renderRows([REVEALER], <MountLater entry={HIDDEN} after={SETTINGS_REVEALER_GRACE_MS + 200} />);
  act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
  expect(marked(REVEALER)).toBe(true);
  act(() => vi.advanceTimersByTime(200));
  expect(marked(HIDDEN)).toBe(true);
  expect(marked(REVEALER)).toBe(false);
});

it("doesn't stand in once the navigation's window has passed", () => {
  renderRows([], <MountLater entry={REVEALER} after={SETTINGS_REVEAL_WINDOW_MS - 100} />);
  act(() => vi.advanceTimersByTime(SETTINGS_REVEAL_WINDOW_MS - 100));
  expect(document.querySelector(`[data-settings-item="${REVEALER.id}"]`)).not.toBeNull();
  act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
  expect(marked(REVEALER)).toBe(false);
});
