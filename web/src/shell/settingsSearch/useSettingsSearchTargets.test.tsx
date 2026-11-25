import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useShortcutPageContext } from "@/components/KeyboardShortcutsDialog";
import type * as CuratedOutHarnessesModule from "@/hooks/useCuratedOutHarnesses";
import type { ServerInfo } from "@/lib/capabilities";
import { CapabilitiesContext } from "@/lib/CapabilitiesContext";
import { writeSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";
import { shortcutPageGroups } from "@/lib/shortcuts/shortcutPage";
import { searchSettings } from "@/pages/settings/search/settingsSearchIndex";

import { useSettingsSearchTargets } from "./useSettingsSearchTargets";

const curation = vi.hoisted(() => ({ curatedOut: new Set<string>() }));
vi.mock("@/hooks/useCuratedOutHarnesses", async (importOriginal) => ({
  ...(await importOriginal<typeof CuratedOutHarnessesModule>()),
  useCuratedOutHarnesses: () => curation.curatedOut,
}));

const HARNESSES = NATIVE_CODING_AGENTS.map((spec) => spec.harness);

function settingsWrapper(info: ServerInfo | "loading") {
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesContext.Provider value={info}>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/settings/general"]}>{children}</MemoryRouter>
      </QueryClientProvider>
    </CapabilitiesContext.Provider>
  );
}

function searchTargets(info: ServerInfo | "loading" = "loading") {
  const wrapper = settingsWrapper(info);
  return renderHook(() => useSettingsSearchTargets(), { wrapper }).result.current;
}

/** The shortcut of every row the Keyboard shortcuts page lists, read from the page itself. */
function pageShortcutIds(info: ServerInfo | "loading") {
  const wrapper = settingsWrapper(info);
  const context = renderHook(() => useShortcutPageContext(), { wrapper }).result.current;
  return shortcutPageGroups(context).flatMap((group) => group.items.map((row) => row.shortcutId));
}

/** The harnesses search offers a row of, by their entries' harness. */
function offeredHarnesses() {
  return new Set(searchTargets().targets.flatMap((target) => target.entry.harness ?? []));
}

beforeEach(() => {
  curation.curatedOut = new Set();
});

describe("useSettingsSearchTargets and harness curation", () => {
  it("offers every harness's rows when the deployment curates none out", () => {
    expect([...offeredHarnesses()].sort()).toEqual([...HARNESSES].sort());
    expect(searchTargets().sections.has("harnesses")).toBe(true);
  });

  it("leaves out the rows of a harness the deployment curates out, and keeps the rest", () => {
    const [curated, ...kept] = HARNESSES;
    curation.curatedOut = new Set([curated]);
    const offered = offeredHarnesses();
    expect(offered.has(curated)).toBe(false);
    expect([...offered].sort()).toEqual([...kept].sort());
    // Its card on the grid goes too, with the card's actions; the kept harnesses' cards stay.
    const ids = searchTargets().targets.map((target) => target.entry.id);
    expect(ids).not.toContain(`harness-${curated}`);
    for (const harness of kept) expect(ids).toContain(`harness-${harness}`);
  });

  it("leaves out the Harnesses section when the deployment curates out every harness", () => {
    curation.curatedOut = new Set(HARNESSES);
    const { targets, sections } = searchTargets();
    expect(sections.has("harnesses")).toBe(false);
    expect(targets.filter((target) => target.entry.section === "harnesses")).toEqual([]);
  });
});

describe("useSettingsSearchTargets and the Keyboard shortcuts page", () => {
  it("offers every row the page lists, the composer's keys included", () => {
    const { targets, shortcutRows } = searchTargets();
    const shortcutIds = targets.flatMap((target) => target.entry.shortcutId ?? []);
    expect(shortcutIds.toSorted()).toEqual(pageShortcutIds("loading").toSorted());
    expect([...shortcutRows.keys()].toSorted()).toEqual(shortcutIds.toSorted());
    expect(shortcutIds).toEqual(
      expect.arrayContaining(["composer.send", "slash.apply", "response.stop"]),
    );
  });

  describe("when the send preference changes while Settings is open", () => {
    afterEach(() => writeSubmitWithModEnter(false));

    it("finds Send message by the new send key on the next render", () => {
      Object.defineProperty(navigator, "platform", { value: "MacIntel", configurable: true });
      const hook = renderHook(() => useSettingsSearchTargets(), {
        wrapper: settingsWrapper("loading"),
      });
      const firstForModEnter = () => {
        const { targets, matchers } = hook.result.current;
        return searchSettings("cmd enter", targets, matchers)[0]?.target.entry.shortcutId;
      };
      expect(firstForModEnter()).toBe("composer.sendWithQueue");

      writeSubmitWithModEnter(true);
      hook.rerender();
      expect(firstForModEnter()).toBe("composer.send");
    });
  });
});
