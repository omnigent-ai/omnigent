// A row revealed through another row's tab list choice, as a plugin's own page is through its
// harness's Plugins tab: the stand-in focuses that tab once it's the selected one, whatever its
// count, and never selects or opens anything itself.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { computeAccessibleName } from "dom-accessibility-api";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { type SettingsEntry, settingsEntry } from "./catalog/types";
import { SettingsItem, useSettingsNavigationClock } from "./SettingsItem";
import { settingsControlProps } from "./settingsControl";
import { SETTINGS_HIGHLIGHT_ATTRIBUTE, SETTINGS_REVEALER_GRACE_MS } from "./settingsReveal";

const PAGES = settingsEntry({
  id: "fixture.pages",
  section: "harnesses",
  label: "Pages",
  description: "A harness's views.",
  parts: { tab: "Pages view" },
  partChoices: { tab: ["MCP servers", "Plugins"] },
  choiceTemplates: {
    tab: {
      "MCP servers": { before: "MCP servers · ", count: true },
      Plugins: { before: "Plugins · ", count: true },
    },
  },
});
const PLUGIN: SettingsEntry = {
  id: "fixture.plugin",
  section: "harnesses",
  label: "Plugin",
  description: "One plugin's page.",
  revealedBy: { entry: PAGES.id, part: "tab", choice: "Plugins" },
};

vi.mock("./catalog", () => ({
  settingsEntryById: (id: string) => [PAGES, PLUGIN].find((entry) => entry.id === id),
}));

function Clock({ children }: { children: ReactNode }) {
  useSettingsNavigationClock();
  return children;
}

const onValueChange = vi.fn();

/** The harness's views, the Plugins tab named as it loads ("Plugins") or with its count. */
function PagesRow({ selected, plugins }: { selected: string; plugins: number | null }) {
  return (
    <SettingsItem entry={PAGES}>
      <span>Pages</span>
      <Tabs value={selected} onValueChange={onValueChange}>
        <TabsList {...settingsControlProps(PAGES, "tab")}>
          <TabsTrigger value="mcps">MCP servers · 1</TabsTrigger>
          <TabsTrigger value="plugins">
            {plugins === null ? "Plugins" : `Plugins · ${plugins}`}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="mcps">Servers</TabsContent>
        <TabsContent value="plugins">Plugin list</TabsContent>
      </Tabs>
    </SettingsItem>
  );
}

let testKey = 0;
function renderAt(row: ReactNode) {
  testKey += 1;
  const tree = (content: ReactNode) => (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter
        initialEntries={[
          { pathname: "/settings/harnesses", hash: `#${PLUGIN.id}`, key: `choice-${testKey}` },
        ]}
      >
        <Clock>{content}</Clock>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(row));
  return { rerender: (next: ReactNode) => view.rerender(tree(next)) };
}

const pluginsTab = () => screen.getByRole("tab", { name: /^Plugins/ });
const marked = () =>
  document
    .querySelector(`[data-settings-item="${PAGES.id}"]`)
    ?.hasAttribute(SETTINGS_HIGHLIGHT_ATTRIBUTE) ?? false;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  Element.prototype.scrollIntoView = () => {};
  onValueChange.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("a revealer's tab list choice", () => {
  it("focuses the choice's tab once the hidden row hasn't appeared, while loading or counted", () => {
    for (const plugins of [null, 0, 3]) {
      renderAt(<PagesRow selected="plugins" plugins={plugins} />);
      act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
      expect(marked()).toBe(true);
      expect(pluginsTab()).toHaveFocus();
      expect(onValueChange).not.toHaveBeenCalled();
      cleanup();
    }
  });

  it("waits while another tab is selected, and focuses the choice once the page shows it", async () => {
    const { rerender } = renderAt(<PagesRow selected="mcps" plugins={2} />);
    act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
    // Focusing the Plugins tab would select it, so nothing is focused yet.
    expect(pluginsTab()).not.toHaveFocus();
    expect(screen.getByRole("tab", { name: "MCP servers · 1" })).not.toHaveFocus();
    await act(async () => rerender(<PagesRow selected="plugins" plugins={2} />));
    expect(pluginsTab()).toHaveFocus();
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it("stops waiting once focus moves elsewhere", async () => {
    const { rerender } = renderAt(
      <>
        <button type="button">Elsewhere</button>
        <PagesRow selected="mcps" plugins={2} />
      </>,
    );
    act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
    await act(async () =>
      rerender(
        <>
          <button type="button">Elsewhere</button>
          <PagesRow selected="plugins" plugins={2} />
        </>,
      ),
    );
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  });

  it("doesn't take a tab whose name only looks like the choice", () => {
    renderAt(<PagesRow selected="mcps" plugins={2} />);
    // "MCP servers · 1" is selected; it counts another choice, so it isn't Plugins.
    act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
    expect(document.activeElement).toBe(document.body);
  });

  describe("matched by the tab's accessible name", () => {
    /** The row with a written-out tab list whose one selected tab is `tab`. */
    // A label naming the tab sits outside the row, as a page heading might.
    const RawRow = ({ tab, label }: { tab: ReactNode; label?: ReactNode }) => (
      <>
        {label}
        <SettingsItem entry={PAGES}>
          <span>Pages</span>
          <div role="tablist" {...settingsControlProps(PAGES, "tab")}>
            {tab}
          </div>
        </SettingsItem>
      </>
    );
    const selected = (props: Record<string, unknown>, children: ReactNode) => (
      <button type="button" role="tab" aria-selected="true" {...props}>
        {children}
      </button>
    );
    const SHAPES: [string, ReactNode, ReactNode, boolean][] = [
      [
        "labelled by another element",
        selected({ "aria-labelledby": "plugins-label" }, "Tab"),
        <span key="l" id="plugins-label">
          Plugins · 2
        </span>,
        true,
      ],
      [
        "with a hidden glyph",
        selected({}, [
          <span key="g" aria-hidden="true">
            ●
          </span>,
          "Plugins · 2",
        ]),
        null,
        true,
      ],
      [
        "named otherwise than its text",
        selected({ "aria-label": "MCP servers · 1" }, "Plugins"),
        null,
        false,
      ],
      [
        "with a hidden icon",
        selected({}, [
          "Plugins · 2",
          <span key="i" hidden>
            icon
          </span>,
        ]),
        null,
        true,
      ],
      [
        "with an icon that isn't displayed",
        selected({}, [
          "Plugins · 2",
          <span key="i" style={{ display: "none" }}>
            icon
          </span>,
        ]),
        null,
        true,
      ],
      [
        "labelled by a hidden label",
        selected({ "aria-labelledby": "plugins-label" }, "Tab"),
        <span key="l" id="plugins-label" aria-hidden="true">
          Plugins · 2
        </span>,
        true,
      ],
      [
        "labelled by an element with its own label",
        selected({ "aria-labelledby": "plugins-label" }, "Tab"),
        <span key="l" id="plugins-label" aria-label="Plugins · 2">
          icon
        </span>,
        true,
      ],
      [
        "with a labelled part",
        selected({}, <span aria-label="Plugins · 2">icon</span>),
        null,
        true,
      ],
    ];

    it.each(SHAPES)("%s", (_shape, tab, label, focused) => {
      renderAt(<RawRow tab={tab} label={label} />);
      const element = screen.getByRole("tab");
      // Focused exactly when the audit names it Plugins, counted or not.
      expect(/^Plugins( · \d+)?$/.test(computeAccessibleName(element))).toBe(focused);
      act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
      expect(element === document.activeElement).toBe(focused);
    });

    it("focuses the tab once its name becomes the choice: its label, its text, or its labelling text or id", async () => {
      const changes: [ReactNode, ReactNode, (tab: HTMLElement) => void][] = [
        [
          selected({ "aria-label": "MCP servers · 1" }, "Tab"),
          null,
          (tab) => tab.setAttribute("aria-label", "Plugins · 1"),
        ],
        [
          selected({}, "MCP servers · 1"),
          null,
          (tab) => (tab.firstChild!.textContent = "Plugins · 1"),
        ],
        [
          selected({ "aria-labelledby": "plugins-label" }, "Tab"),
          <span key="l" id="plugins-label">
            MCP servers · 1
          </span>,
          () => (document.getElementById("plugins-label")!.firstChild!.textContent = "Plugins · 1"),
        ],
        [
          // Part of a label outside the row becomes hidden, leaving its name Plugins.
          selected({ "aria-labelledby": "plugins-label" }, "Tab"),
          <span key="l" id="plugins-label">
            Plugins · 1<span id="label-extra"> (loading)</span>
          </span>,
          () => document.getElementById("label-extra")!.setAttribute("hidden", ""),
        ],
        [
          // Or stops being displayed, by its style.
          selected({ "aria-labelledby": "plugins-label" }, "Tab"),
          <span key="l" id="plugins-label">
            Plugins · 1<span id="label-extra"> (loading)</span>
          </span>,
          () => (document.getElementById("label-extra")!.style.display = "none"),
        ],
        [
          // Or by a class a stylesheet hides.
          selected({ "aria-labelledby": "plugins-label" }, "Tab"),
          <span key="l" id="plugins-label">
            <style>{".label-gone { display: none; }"}</style>
            Plugins · 1<span id="label-extra"> (loading)</span>
          </span>,
          () => document.getElementById("label-extra")!.classList.add("label-gone"),
        ],
        [
          // A label that exists already, outside the row, takes the id the tab names.
          selected({ "aria-labelledby": "plugins-label" }, "MCP servers · 1"),
          <span key="l" id="other-label">
            Plugins · 1
          </span>,
          () => (document.getElementById("other-label")!.id = "plugins-label"),
        ],
      ];
      for (const [tab, label, change] of changes) {
        renderAt(<RawRow tab={tab} label={label} />);
        act(() => vi.advanceTimersByTime(SETTINGS_REVEALER_GRACE_MS));
        const element = screen.getByRole("tab");
        expect(element).not.toHaveFocus();
        // oxlint-disable-next-line no-await-in-loop -- each change rendered and checked in turn
        await act(async () => change(element));
        expect(element).toHaveFocus();
        cleanup();
      }
    });
  });
});
