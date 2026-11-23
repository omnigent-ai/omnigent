// A part whose control is a tab list: owned by its row through ui/'s tabs, each tab one of the
// part's choices, and revealed by focusing its selected tab without selecting another.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { auditSettingsDom } from "@/test/settingsDomAudit";

import { settingsEntry } from "./catalog/types";
import { SettingsItem, useSettingsNavigationClock } from "./SettingsItem";
import { settingsControlProps } from "./settingsControl";

const PAGES = settingsEntry({
  id: "fixture.pages",
  section: "general",
  label: "Pages",
  description: "A page's views.",
  parts: { tab: "Pages view" },
  partChoices: { tab: ["Settings", "Servers"] },
  choiceTemplates: { tab: { Servers: { before: "Servers · ", count: true } } },
});
const ENTRIES = new Map([[PAGES.id, PAGES]]);
const IGNORE_SELECTION = () => {};

function Clock({ children }: { children: ReactNode }) {
  useSettingsNavigationClock();
  return children;
}

function renderAt(hash: string, children: ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[{ pathname: "/settings/general", hash }]}>
        <Clock>{children}</Clock>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The row's views, the servers tab named around its count, as a harness page shows them. */
function PagesRow({
  selected = "settings",
  servers = 2,
  disabled = false,
  onValueChange = IGNORE_SELECTION,
}: {
  selected?: string;
  servers?: number | null;
  disabled?: boolean;
  onValueChange?: (value: string) => void;
}) {
  return (
    <SettingsItem entry={PAGES}>
      <span>Pages</span>
      <Tabs value={selected} onValueChange={onValueChange}>
        <TabsList {...settingsControlProps(PAGES, "tab")}>
          <TabsTrigger value="settings" disabled={disabled}>
            Settings
          </TabsTrigger>
          <TabsTrigger value="servers">
            {servers === null ? "Servers" : `Servers · ${servers}`}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="settings">Settings view</TabsContent>
        <TabsContent value="servers">Servers view</TabsContent>
      </Tabs>
    </SettingsItem>
  );
}

const tab = (name: string | RegExp) => screen.getByRole("tab", { name });

afterEach(() => cleanup());

describe("a tab list as a part's control", () => {
  it("is its part's control, its tabs that part's choices, its panels covered active or not", () => {
    for (const servers of [null, 0, 2]) {
      renderAt("", <PagesRow servers={servers} />);
      expect(screen.getByRole("tablist")).toHaveAttribute(
        "data-settings-control",
        "fixture.pages:tab",
      );
      expect(screen.getByRole("tablist")).toHaveAccessibleName("Pages view");
      // Radix keeps the inactive panel in the page, hidden and empty.
      expect(document.querySelectorAll('[role="tabpanel"]')).toHaveLength(2);
      expect(auditSettingsDom({ roots: [document.body], entries: ENTRIES })).toEqual([]);
      cleanup();
    }
  });

  it("reveals by focusing the selected tab, selecting nothing", () => {
    const onValueChange = vi.fn();
    renderAt("#fixture.pages:tab", <PagesRow selected="servers" onValueChange={onValueChange} />);
    act(() => {});
    expect(tab(/^Servers/)).toHaveFocus();
    expect(tab(/^Servers/)).toHaveAttribute("aria-selected", "true");
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it("doesn't focus another tab when the selected one can't take focus, since that selects it", () => {
    const onValueChange = vi.fn();
    renderAt("#fixture.pages:tab", <PagesRow disabled onValueChange={onValueChange} />);
    act(() => {});
    expect(tab(/^Servers/)).not.toHaveFocus();
    expect(tab("Settings")).toHaveAttribute("aria-selected", "true");
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it("doesn't take a selected tab of a tab list nested inside it", () => {
    renderAt(
      "#fixture.pages:tab",
      <SettingsItem entry={PAGES}>
        <span>Pages</span>
        <div role="tablist" {...settingsControlProps(PAGES, "tab")}>
          <div role="tablist" aria-label="Another list">
            <button type="button" role="tab" aria-selected="true">
              Settings
            </button>
          </div>
        </div>
      </SettingsItem>,
    );
    act(() => {});
    expect(tab("Settings")).not.toHaveFocus();
  });

  it("focuses a tab once it becomes the selected one", async () => {
    renderAt(
      "#fixture.pages:tab",
      <SettingsItem entry={PAGES}>
        <span>Pages</span>
        <div role="tablist" {...settingsControlProps(PAGES, "tab")}>
          <button type="button" role="tab" aria-selected="false">
            Settings
          </button>
        </div>
      </SettingsItem>,
    );
    act(() => {});
    expect(tab("Settings")).not.toHaveFocus();
    await act(async () => {
      tab("Settings").setAttribute("aria-selected", "true");
    });
    expect(tab("Settings")).toHaveFocus();
  });

  it("waits for the selected tab to take focus, and stops once focus moves elsewhere", async () => {
    const first = renderAt("#fixture.pages:tab", <PagesRow disabled />);
    act(() => {});
    expect(tab("Settings")).not.toHaveFocus();
    first.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter
          initialEntries={[{ pathname: "/settings/general", hash: "#fixture.pages:tab" }]}
        >
          <Clock>
            <PagesRow />
          </Clock>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await act(async () => {});
    expect(tab("Settings")).toHaveFocus();
    cleanup();

    const second = renderAt(
      "#fixture.pages:tab",
      <>
        <button type="button">Elsewhere</button>
        <PagesRow disabled />
      </>,
    );
    act(() => {});
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
    second.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter
          initialEntries={[{ pathname: "/settings/general", hash: "#fixture.pages:tab" }]}
        >
          <Clock>
            <button type="button">Elsewhere</button>
            <PagesRow />
          </Clock>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await act(async () => {});
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  });
});
