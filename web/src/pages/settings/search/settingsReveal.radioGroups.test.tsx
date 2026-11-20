// A row whose control is a radio group: owned by its row through the shared radio components,
// each radio one of its choices, and revealed by focusing the radio a keyboard user would land on.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";
import { type ReactNode, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { CardRadioGroup } from "@/components/theme/CardRadioGroup";
import { IconSegmentedControl } from "@/components/theme/IconSegmentedControl";

import { TERMINAL_THEME_ENTRY, THEME_MODE_ENTRY } from "./catalog/appearance";
import type { SettingsEntry } from "./catalog/types";
import { SettingsItem, useSettingsNavigationClock } from "./SettingsItem";
import { settingsControlProps, settingsLabelId } from "./settingsControl";
import { SettingsOwner } from "./settingsOwnership";

const ICONS = [MonitorIcon, SunIcon, MoonIcon];

function Clock({ children }: { children: ReactNode }) {
  useSettingsNavigationClock();
  return children;
}

function renderAt(hash: string, children: ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[{ pathname: "/settings/appearance", hash }]}>
        <Clock>{children}</Clock>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A row whose choices are icon radios, as Mode and Terminal theme are. */
function IconRow({
  entry,
  labels,
  initial,
}: {
  entry: SettingsEntry;
  labels: string[];
  initial: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SettingsItem entry={entry}>
      <span id={settingsLabelId(entry)}>{entry.label}</span>
      <SettingsOwner entry={entry}>
        <IconSegmentedControl
          labelledBy={settingsLabelId(entry)}
          value={value}
          onSelect={setValue}
          items={labels.map((label, index) => ({
            value: label,
            label,
            icon: ICONS[index]!,
            testId: `${entry.id}-${label}`,
          }))}
        />
      </SettingsOwner>
    </SettingsItem>
  );
}

const MODE = (
  <IconRow entry={THEME_MODE_ENTRY} labels={["System", "Light", "Dark"]} initial="Light" />
);
const TERMINAL = (
  <IconRow entry={TERMINAL_THEME_ENTRY} labels={["Match app", "Light", "Dark"]} initial="Dark" />
);
const radioIn = (entry: SettingsEntry, name: string) =>
  [
    ...document.querySelectorAll<HTMLElement>(`[data-settings-item="${entry.id}"] [role="radio"]`),
  ].find((radio) => radio.getAttribute("aria-label") === name)!;

afterEach(() => cleanup());

describe("a radio group as its row's control", () => {
  it("is each row's own control, its radios that row's choices, names unchanged", () => {
    renderAt(
      "",
      <>
        {MODE}
        {TERMINAL}
      </>,
    );
    const groups = screen.getAllByRole("radiogroup");
    expect(groups.map((group) => group.getAttribute("data-settings-control"))).toEqual([
      "theme-mode:",
      "terminal-theme:",
    ]);
    expect(groups[0]).toHaveAccessibleName("Mode");
    expect(groups[1]).toHaveAccessibleName("Terminal theme");
  });

  it("keeps its own label outside a Settings owner", () => {
    render(
      <>
        <span id="outside-label">Outside</span>
        <CardRadioGroup
          labelledBy="outside-label"
          value="a"
          onSelect={() => {}}
          items={[{ value: "a", testId: "a", body: "A" }]}
        />
      </>,
    );
    const group = screen.getByRole("radiogroup");
    expect(group).toHaveAccessibleName("Outside");
    expect(group).not.toHaveAttribute("data-settings-control");
  });

  it("reveals by focusing the group's checked radio, in the row the URL names", () => {
    renderAt(
      "#terminal-theme",
      <>
        {MODE}
        {TERMINAL}
      </>,
    );
    act(() => {});
    expect(radioIn(TERMINAL_THEME_ENTRY, "Dark")).toHaveFocus();
    // Revealing changes nothing it focuses.
    expect(radioIn(TERMINAL_THEME_ENTRY, "Dark")).toHaveAttribute("aria-checked", "true");
    expect(radioIn(THEME_MODE_ENTRY, "Light")).toHaveAttribute("aria-checked", "true");
  });

  it("moves the choice with arrow keys within its own group only", () => {
    renderAt(
      "#theme-mode",
      <>
        {MODE}
        {TERMINAL}
      </>,
    );
    act(() => {});
    expect(radioIn(THEME_MODE_ENTRY, "Light")).toHaveFocus();
    fireEvent.keyDown(radioIn(THEME_MODE_ENTRY, "Light"), { key: "ArrowRight" });
    expect(radioIn(THEME_MODE_ENTRY, "Dark")).toHaveAttribute("aria-checked", "true");
    expect(radioIn(THEME_MODE_ENTRY, "Dark")).toHaveFocus();
    expect(radioIn(TERMINAL_THEME_ENTRY, "Dark")).toHaveAttribute("aria-checked", "true");
    expect(radioIn(TERMINAL_THEME_ENTRY, "Light")).toHaveAttribute("aria-checked", "false");
  });

  describe("when its checked radio can't take focus", () => {
    /** Terminal theme's row as raw markup, so a radio can be disabled or nested. */
    function RawRow({ children }: { children: ReactNode }) {
      return (
        <SettingsItem entry={TERMINAL_THEME_ENTRY}>
          <span id={settingsLabelId(TERMINAL_THEME_ENTRY)}>Terminal theme</span>
          <div role="radiogroup" {...settingsControlProps(TERMINAL_THEME_ENTRY)}>
            {children}
          </div>
        </SettingsItem>
      );
    }
    const radio = (name: string, props: Record<string, unknown> = {}) => (
      <button
        type="button"
        role="radio"
        aria-label={name}
        aria-checked="false"
        tabIndex={-1}
        {...props}
      >
        {name}
      </button>
    );

    it("focuses the group's first enabled radio", () => {
      renderAt(
        "#terminal-theme",
        <RawRow>
          {radio("Match app", { disabled: true, "aria-checked": "true" })}
          {radio("Light", { "aria-disabled": "true" })}
          {radio("Dark")}
        </RawRow>,
      );
      act(() => {});
      expect(screen.getByRole("radio", { name: "Dark" })).toHaveFocus();
      expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "false");
    });

    it("skips a checked radio hidden from focus", () => {
      renderAt(
        "#terminal-theme",
        <RawRow>
          <span aria-hidden="true">{radio("Match app", { "aria-checked": "true" })}</span>
          {radio("Dark")}
        </RawRow>,
      );
      act(() => {});
      expect(screen.getByRole("radio", { name: "Dark" })).toHaveFocus();
    });

    it("waits for a radio that's aria-disabled, and focuses it once enabled", async () => {
      renderAt(
        "#terminal-theme",
        <RawRow>{radio("Dark", { "aria-disabled": "true", "aria-checked": "true" })}</RawRow>,
      );
      act(() => {});
      const dark = screen.getByRole("radio", { name: "Dark" });
      expect(dark).not.toHaveFocus();
      await act(async () => {
        dark.removeAttribute("aria-disabled");
      });
      expect(dark).toHaveFocus();
    });

    describe("with native radios", () => {
      const native = (value: string, props: Record<string, unknown> = {}) => (
        <label key={value}>
          <input type="radio" name="native-theme" value={value} readOnly {...props} />
          {value}
        </label>
      );

      it.each(["Light", "Dark"])("focuses the checked one, %s, leaving it checked", (value) => {
        renderAt(
          "#terminal-theme",
          <RawRow>
            {["Light", "Dark"].map((name) => native(name, { checked: name === value }))}
          </RawRow>,
        );
        act(() => {});
        const target = screen.getByRole("radio", { name: value });
        expect(target).toHaveFocus();
        expect(target).toBeChecked();
      });

      it("skips a disabled one, checked or not, for the first enabled one", () => {
        renderAt(
          "#terminal-theme",
          <RawRow>
            {native("Match app", { checked: true, disabled: true })}
            {native("Light", { disabled: true })}
            {native("Dark", { checked: false })}
          </RawRow>,
        );
        act(() => {});
        expect(screen.getByRole("radio", { name: "Dark" })).toHaveFocus();
        expect(screen.getByRole("radio", { name: "Dark" })).not.toBeChecked();
      });

      it("skips every radio of a disabled fieldset", () => {
        renderAt(
          "#terminal-theme",
          <RawRow>
            <fieldset disabled>{native("Dark", { checked: true })}</fieldset>
          </RawRow>,
        );
        act(() => {});
        expect(screen.getByRole("radio", { name: "Dark" })).not.toHaveFocus();
      });
    });

    it("doesn't take a radio of a group nested inside it", () => {
      renderAt(
        "#terminal-theme",
        <RawRow>
          <div role="radiogroup" aria-label="Another group">
            {radio("Light", { "aria-checked": "true" })}
          </div>
        </RawRow>,
      );
      act(() => {});
      expect(screen.getByRole("radio", { name: "Light" })).not.toHaveFocus();
    });
  });
});
