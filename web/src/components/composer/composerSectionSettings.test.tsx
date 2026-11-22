import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { currentChoiceLabel } from "@/store/paletteSettings";
import {
  chooseSettingValue,
  expectSetting,
  registeredPaletteCommands,
} from "@/test/paletteCommands";

import type { ComposerConfigSection } from "./ComposerConfigSections";
import {
  ComposerPaletteSettings,
  CURRENT_ONLY_REASON,
  EFFORT_BUSY_REASON,
  EFFORT_ROUTED_REASON,
  effortSetting,
  MODEL_BUSY_REASON,
  modelSetting,
} from "./composerSectionSettings";

function section(
  header: string,
  choices: { key: string; label: string; checked?: boolean; disabled?: boolean }[],
): ComposerConfigSection {
  return {
    testId: "efforts",
    header,
    choices: choices.map((choice) => ({
      checked: false,
      ...choice,
      onSelect: vi.fn(),
    })),
  };
}

describe("effortSetting", () => {
  it("offers none for a harness without an effort setting", () => {
    expect(effortSetting(undefined, "composer-1")).toBeNull();
  });

  it("lists every level, shows the current one, and runs the menu's own action", () => {
    const efforts = section("Effort", [
      { key: "low", label: "Low" },
      { key: "high", label: "High", checked: true },
    ]);
    const setting = effortSetting(efforts, "composer-1")!;
    expect(setting).toMatchObject({ label: "Reasoning Effort", owner: "composer-1" });
    expect(setting.choices.map((choice) => choice.label)).toEqual(["Low", "High"]);
    expect(currentChoiceLabel(setting)).toBe("High");
    void chooseSettingValue(setting, "Low");
    expect(efforts.choices[0]!.onSelect).toHaveBeenCalledOnce();
  });

  it("names a thinking level as the menu does, and explains a level that can't be chosen now", () => {
    const setting = effortSetting(
      section("Thinking level", [{ key: "medium", label: "Medium", disabled: true }]),
      "composer-1",
    )!;
    expect(setting.label).toBe("Thinking Level");
    expect(setting.choices).toMatchObject([
      { label: "Medium", availability: { available: false, reason: EFFORT_BUSY_REASON } },
    ]);
  });

  it("shows no value while routing picks the effort, and skips rows the menu can't select", () => {
    const efforts = section("Effort", [{ key: "low", label: "Low" }]);
    efforts.choices.push({ key: "note", label: "Routing picks the effort", checked: false });
    const setting = effortSetting(efforts, "composer-1")!;
    expect(setting.choices.map((choice) => choice.value)).toEqual(["low"]);
    expect(currentChoiceLabel(setting)).toBeUndefined();
  });
});

describe("modelSetting", () => {
  it("lists the menu's models with their tooltip, and a busy model says the model can't change", () => {
    const models: ComposerConfigSection = {
      testId: "models",
      header: "Models",
      choices: [
        {
          key: "opus",
          label: "Opus",
          checked: true,
          title: "Best for hard tasks",
          onSelect: vi.fn(),
        },
        {
          key: "haiku",
          label: "Haiku",
          checked: false,
          disabled: true,
          title: "Fast",
          onSelect: vi.fn(),
        },
      ],
    };
    const setting = modelSetting(models, "composer-1")!;
    expect(setting).toMatchObject({ id: "composer-model", label: "Model", owner: "composer-1" });
    expect(currentChoiceLabel(setting)).toBe("Opus");
    expect(setting.choices).toMatchObject([
      { label: "Opus", description: "Best for hard tasks", availability: { available: true } },
      { label: "Haiku", availability: { available: false, reason: MODEL_BUSY_REASON } },
    ]);
    void chooseSettingValue(setting, "Opus");
    expect(models.choices[0]!.onSelect).toHaveBeenCalledOnce();
  });

  it("shows a current model the menu can't select again, greyed, as the current value", () => {
    const models: ComposerConfigSection = {
      testId: "models",
      header: "Models",
      choices: [
        { key: "opus", label: "Opus", checked: false, onSelect: vi.fn() },
        { key: "__current__", label: "Retired model (current)", checked: true, disabled: true },
      ],
    };
    const setting = modelSetting(models, "composer-1")!;
    expect(currentChoiceLabel(setting)).toBe("Retired model (current)");
    expect(setting.choices[1]!.availability).toEqual({
      available: false,
      reason: CURRENT_ONLY_REASON,
    });
  });
});

describe("modelSetting with Smart Routing", () => {
  const models = (): ComposerConfigSection => ({
    testId: "models",
    header: "Models",
    choices: [
      { key: "__default__", label: "Default", checked: false, onSelect: vi.fn() },
      { key: "opus", label: "Opus", checked: false, onSelect: vi.fn() },
    ],
  });

  it("keeps Smart Routing when no fixed model can be chosen, as the menu does", async () => {
    const select = vi.fn();
    const empty: ComposerConfigSection = { testId: "models", header: "Models", choices: [] };
    const setting = modelSetting(empty, "composer-1", { active: false, select });
    expect(setting?.choices.map((choice) => choice.label)).toEqual(["Smart Routing"]);
    await chooseSettingValue(setting!, "Smart Routing");
    expect(select).toHaveBeenCalledOnce();
    // Without routing there's nothing to choose, so no setting.
    expect(modelSetting(empty, "composer-1")).toBeNull();
  });

  it("lists Smart Routing first, current while it routes, choosing it as the menu's row does", () => {
    const select = vi.fn();
    const setting = modelSetting(models(), "composer-1", { active: true, select })!;
    expect(setting.choices.map((choice) => choice.label)).toEqual([
      "Smart Routing",
      "Default",
      "Opus",
    ]);
    expect(currentChoiceLabel(setting)).toBe("Smart Routing");
    void chooseSettingValue(setting, "Smart Routing");
    expect(select).toHaveBeenCalledOnce();
  });

  it("chooses a fixed model through the menu's own handler while routing, and nothing else", () => {
    const modelSection = models();
    const select = vi.fn();
    const setting = modelSetting(modelSection, "composer-1", { active: true, select })!;
    void chooseSettingValue(setting, "Opus");
    expect(modelSection.choices[1]!.onSelect).toHaveBeenCalledOnce();
    expect(select).not.toHaveBeenCalled();
  });

  it("greys Smart Routing with the composer's reason while the model can't change", () => {
    const setting = modelSetting(models(), "composer-1", {
      active: false,
      unavailableReason: MODEL_BUSY_REASON,
      select: vi.fn(),
    })!;
    expect(setting.choices[0]!.availability).toEqual({
      available: false,
      reason: MODEL_BUSY_REASON,
    });
    expect(currentChoiceLabel(setting)).toBeUndefined();
  });
});

describe("ComposerPaletteSettings, as the open session's picker mounts it", () => {
  const levels = (header: string): ComposerConfigSection => ({
    testId: "efforts",
    header,
    choices: [
      { key: "low", label: "Low", checked: true, onSelect: vi.fn() },
      { key: "high", label: "High", checked: false, onSelect: vi.fn() },
    ],
  });
  const models = (): ComposerConfigSection => ({
    testId: "models",
    header: "Models",
    choices: [
      { key: "opus", label: "Opus", checked: true, onSelect: vi.fn() },
      { key: "haiku", label: "Haiku", checked: false, onSelect: vi.fn() },
    ],
  });
  const mount = (props: Partial<Parameters<typeof ComposerPaletteSettings>[0]>, flag = true) =>
    render(
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
      >
        <ComposerPaletteSettings
          source="composer:c1"
          models={undefined}
          efforts={undefined}
          effortRouted={false}
          {...props}
        />
      </CapabilitiesProvider>,
    );

  afterEach(() => {
    cleanup();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  it("lists Model and the effort under Composer, and chooses through the menu's handlers", async () => {
    const modelSection = models();
    const effortSection = levels("Effort");
    mount({ models: modelSection, efforts: effortSection });
    const commands = registeredPaletteCommands();
    expect(
      Object.values(usePaletteCommandsStore.getState().groups).map((group) => group.heading),
    ).toEqual(["Composer", "Composer"]);
    await chooseSettingValue(
      expectSetting(commands, "Model", { current: "Opus", choices: ["Opus", "Haiku"] }),
      "Haiku",
    );
    expect(modelSection.choices[1]!.onSelect).toHaveBeenCalledOnce();
    await chooseSettingValue(
      expectSetting(commands, "Reasoning Effort", { current: "Low", choices: ["Low", "High"] }),
      "High",
    );
    expect(effortSection.choices[1]!.onSelect).toHaveBeenCalledOnce();
  });

  it("registers Pi's thinking level under its own name", () => {
    mount({ efforts: levels("Thinking level") });
    expectSetting(registeredPaletteCommands(), "Thinking Level", {
      current: "Low",
      choices: ["Low", "High"],
    });
  });

  it("greys the whole effort setting while Smart Routing picks the effort", () => {
    mount({ efforts: levels("Effort"), effortRouted: true });
    expect(
      registeredPaletteCommands().find((command) => command.label === "Reasoning Effort")
        ?.availability,
    ).toEqual({ available: false, reason: EFFORT_ROUTED_REASON });
  });

  it("keeps its rows when a re-render rebuilds the same choices, and chooses from the latest", async () => {
    const { rerender } = mount({ models: models() });
    const listed = registeredPaletteCommands()[0];
    const rebuilt = models();
    rerender(
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: true } }}
      >
        <ComposerPaletteSettings
          source="composer:c1"
          models={rebuilt}
          efforts={undefined}
          effortRouted={false}
        />
      </CapabilitiesProvider>,
    );
    expect(registeredPaletteCommands()[0]).toBe(listed);
    await chooseSettingValue(listed!.setting!, "Haiku");
    expect(rebuilt.choices[1]!.onSelect).toHaveBeenCalledOnce();
  });

  it("registers nothing with quick_pick_palette off", () => {
    mount({ models: models(), efforts: levels("Effort") }, false);
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
