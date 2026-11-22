// The composer's model and reasoning effort as ⌘K settings. Their values are the choices of
// the menu sections the composer's picker renders, so a value and its menu row share their
// label, check, availability and action.

import { BoxIcon, GaugeIcon, type LucideIcon } from "lucide-react";
import { useId, useMemo, useRef } from "react";

import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import { AVAILABLE, type PaletteCommand, unavailableBecause } from "@/store/paletteCommands";
import { type PaletteSetting, settingCommand } from "@/store/paletteSettings";

import type { ComposerConfigSection } from "./ComposerConfigSections";
import { COMPOSER_PALETTE_HEADING } from "./useComposerAddPaletteCommands";

export const EFFORT_BUSY_REASON = "The effort can't change right now";
export const MODEL_BUSY_REASON = "The model can't change right now";
/** Why the effort can't change while Smart Routing picks it. */
export const EFFORT_ROUTED_REASON = "Smart Routing picks the effort for each turn";
/** A current value the menu shows but can't select again, such as a model no longer listed. */
export const CURRENT_ONLY_REASON = "This is the current value; it can't be chosen again here";
/** Smart Routing as a Model value; model keys never take this form. */
export const SMART_ROUTING_MODEL_VALUE = "smart-routing:";

interface SectionSettingKind {
  id: string;
  label: string;
  icon: LucideIcon;
  keywords: readonly string[];
  busyReason: string;
  /** What a choice's `title` says: why it's disabled (effort), or what it is (a model's tooltip). */
  titleIs: "reason" | "description";
}

function choiceLabel(choice: ComposerConfigSection["choices"][number]): string {
  return typeof choice.label === "string" ? choice.label : choice.key;
}

function choiceAvailability(
  choice: ComposerConfigSection["choices"][number],
  kind: SectionSettingKind,
) {
  if (!choice.onSelect) return unavailableBecause(CURRENT_ONLY_REASON);
  if (!choice.disabled) return AVAILABLE;
  const reason = kind.titleIs === "reason" ? choice.title : undefined;
  return unavailableBecause(reason ?? kind.busyReason);
}

/**
 * `section` as a ⌘K setting of the composer `owner`: every choice the menu can select,
 * plus the checked one when the menu only shows it. None without a selectable choice, unless
 * the caller adds one, as Smart Routing does for models.
 */
export function composerSectionSetting(
  section: ComposerConfigSection | undefined,
  owner: string,
  kind: SectionSettingKind,
  { addsChoice = false }: { addsChoice?: boolean } = {},
): PaletteSetting | null {
  if (!section) return null;
  const listed = section.choices.filter((choice) => choice.onSelect || choice.checked);
  if (!addsChoice && !listed.some((choice) => choice.onSelect)) return null;
  return {
    id: kind.id,
    owner,
    label: kind.label,
    icon: kind.icon,
    keywords: kind.keywords,
    // None checked while routing picks the value, so the row shows no value.
    current: listed.find((choice) => choice.checked)?.key ?? "",
    choices: listed.map((choice) => ({
      value: choice.key,
      label: choiceLabel(choice),
      description: kind.titleIs === "description" ? choice.title : undefined,
      keywords: [choice.key],
      availability: choiceAvailability(choice, kind),
    })),
    choose: (key) => section.choices.find((choice) => choice.key === key)?.onSelect?.(),
  };
}

/** The effort section's setting; Pi calls its effort a thinking level, as its menu's header does. */
export function effortSetting(
  section: ComposerConfigSection | undefined,
  owner: string,
): PaletteSetting | null {
  return composerSectionSetting(section, owner, {
    id: "composer-effort",
    label: section?.header === "Thinking level" ? "Thinking Level" : "Reasoning Effort",
    icon: GaugeIcon,
    keywords: ["effort", "reasoning", "thinking", "level"],
    busyReason: EFFORT_BUSY_REASON,
    titleIs: "reason",
  });
}

/** The composer's Smart Routing row, which its menu shows above the models. */
export interface ModelRouting {
  /** Routing picks the model now. */
  active: boolean;
  /** Why the row can't be chosen now, if it can't. */
  unavailableReason?: string;
  /** Turn routing on. */
  select: () => void;
}

/**
 * The model section's setting. With `routing`, Smart Routing is its first value and the
 * current one while it routes; a fixed model is chosen through the menu's own handler.
 */
export function modelSetting(
  section: ComposerConfigSection | undefined,
  owner: string,
  routing?: ModelRouting,
): PaletteSetting | null {
  // Smart Routing stays choosable when no fixed model is, as the menu's row does.
  const setting = composerSectionSetting(
    section,
    owner,
    {
      id: "composer-model",
      label: "Model",
      icon: BoxIcon,
      keywords: ["model", "llm", "switch model"],
      busyReason: MODEL_BUSY_REASON,
      titleIs: "description",
    },
    { addsChoice: routing !== undefined },
  );
  if (!setting || !routing) return setting;
  return {
    ...setting,
    current: routing.active ? SMART_ROUTING_MODEL_VALUE : setting.current,
    choices: [
      {
        value: SMART_ROUTING_MODEL_VALUE,
        label: SMART_ROUTING_LABEL,
        description: "Picks the model for each turn",
        availability: routing.unavailableReason
          ? unavailableBecause(routing.unavailableReason)
          : AVAILABLE,
      },
      ...setting.choices,
    ],
    choose: (value) => {
      if (value === SMART_ROUTING_MODEL_VALUE) return routing.select();
      // A fixed model's own handler leaves routing once its pin succeeds.
      return setting.choose(value);
    },
  };
}

/** What the setting shows for `section`, so a re-render with the same choices reuses it. */
function sectionSignature(section: ComposerConfigSection | undefined): string {
  if (!section) return "";
  return JSON.stringify([
    typeof section.header === "string" ? section.header : "",
    section.choices.map((choice) => [
      choice.key,
      typeof choice.label === "string" ? choice.label : "",
      choice.checked,
      choice.disabled ?? false,
      choice.title ?? "",
      choice.onSelect !== undefined,
    ]),
  ]);
}

const NO_COMMANDS: PaletteCommand[] = [];

/**
 * Offer `section` in ⌘K under `source` as one setting. `extraSignature` covers what `build`
 * reads beyond the section, such as routing.
 */
function useComposerSectionSetting(
  source: string,
  section: ComposerConfigSection | undefined,
  build: (section: ComposerConfigSection | undefined, owner: string) => PaletteSetting | null,
  extraSignature: string,
): void {
  // The picker rebuilds its sections on every render; choosing reads the latest build.
  const latest = useRef({ section, build });
  latest.current = { section, build };
  const owner = useId();
  const signature = sectionSignature(section) + extraSignature;
  const commands = useMemo(() => {
    const setting = latest.current.build(latest.current.section, owner);
    if (!setting) return NO_COMMANDS;
    return [
      settingCommand({
        ...setting,
        choose: (value) => latest.current.build(latest.current.section, owner)?.choose(value),
      }),
    ];
    // The signature captures everything the setting shows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, owner]);
  useQuickPickPaletteCommands(source, COMPOSER_PALETTE_HEADING, commands);
}

/**
 * The open session composer's Model and Reasoning Effort in ⌘K. The picker mounts this only
 * while quick_pick_palette is on, so with it off none of these hooks run.
 */
export function ComposerPaletteSettings({
  source,
  models,
  efforts,
  effortRouted,
  routing,
}: {
  source: string;
  models: ComposerConfigSection | undefined;
  efforts: ComposerConfigSection | undefined;
  /** Smart Routing picks the effort, which greys the whole setting. */
  effortRouted: boolean;
  routing?: ModelRouting;
}): null {
  useComposerSectionSetting(
    `${source}:effort`,
    efforts,
    (current, owner) => {
      const setting = effortSetting(current, owner);
      return setting && effortRouted
        ? { ...setting, availability: unavailableBecause(EFFORT_ROUTED_REASON) }
        : setting;
    },
    effortRouted ? "routed" : "",
  );
  useComposerSectionSetting(
    `${source}:model`,
    models,
    (current, owner) => modelSetting(current, owner, routing),
    routing ? JSON.stringify([routing.active, routing.unavailableReason ?? ""]) : "",
  );
  return null;
}
