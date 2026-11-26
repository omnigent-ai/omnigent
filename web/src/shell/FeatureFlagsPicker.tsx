import { FlagIcon, RotateCcwIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { type QuickPickItem, QuickPickDialog, useQuickPickPages } from "@/components/ui/quick-pick";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { type FeatureKey, isFeatureEnabled } from "@/lib/capabilities";
import {
  activeUiFeatureOverrides,
  canOverrideUiFeatures,
  clearUiFeatureOverrides,
  OVERRIDABLE_UI_FEATURES,
  readUiFeatureOverrides,
  writeUiFeatureOverride,
} from "@/lib/uiFeatureOverrides";
import { closeFeatureFlagsPicker, useFeatureFlagsPickerStore } from "@/store/featureFlagsPicker";
import { currentChoiceLabel, type PaletteSetting } from "@/store/paletteSettings";

import type { PaletteTarget } from "./paletteTarget";
import { settingFailedStatus } from "./settingChoiceStatus";
import { choiceItemId, pageStatusSummary, settingChoiceItems } from "./settingChoicesPage";

type PickerValue =
  { kind: "flag"; settingId: string } | { kind: "clear-overrides" } | PaletteTarget;

type FeatureFlagSetting = PaletteSetting & { id: FeatureKey };

function featureChoice(override: boolean | undefined): string {
  if (override === undefined) return "automatic";
  return override ? "on" : "off";
}

export function FeatureFlagsPicker() {
  const open = useFeatureFlagsPickerStore((state) => state.open);
  const replaced = useFeatureFlagsPickerStore((state) => state.replaced);
  const info = useServerInfo();
  const pages = useQuickPickPages<"flags" | "choices">("flags");
  const { reset, pop } = pages;
  const [opened, setOpened] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const available =
    info !== "loading" && isFeatureEnabled(info, "quick_pick_palette") && canOverrideUiFeatures();

  useEffect(() => {
    if (open && available) return;
    setOpened(null);
    setStatus(null);
    reset();
  }, [open, available, reset]);

  const settings = useMemo<FeatureFlagSetting[]>(() => {
    if (!available) return [];
    const overrides = readUiFeatureOverrides();
    return OVERRIDABLE_UI_FEATURES.filter(({ feature }) =>
      Object.hasOwn(info.features, feature),
    ).map((entry) => {
      const override = overrides[entry.feature];
      const automatic = info.features[entry.feature] === true ? "On" : "Off";
      return {
        id: entry.feature,
        owner: "app",
        label: entry.label,
        icon: FlagIcon,
        keywords: ["feature flag", entry.feature],
        current: featureChoice(override),
        choices: [
          { value: "automatic", label: `Automatic (${automatic})`, description: entry.description },
          { value: "on", label: "On" },
          { value: "off", label: "Off" },
        ],
        choose: () => undefined,
      };
    });
  }, [available, info]);

  const setting = settings.find((entry) => entry.id === opened);
  const onChoices = pages.page.id === "choices";
  const backToFlags = useCallback(() => {
    setOpened(null);
    setStatus(null);
    pop();
  }, [pop]);
  useEffect(() => {
    if (onChoices && !setting) backToFlags();
  }, [onChoices, setting, backToFlags]);

  let items: QuickPickItem<PickerValue>[];
  if (onChoices) {
    items = setting ? settingChoiceItems(setting.id, setting) : [];
  } else {
    items = settings.map((entry): QuickPickItem<PickerValue> => ({
      id: entry.id,
      label: entry.label,
      description: entry.id,
      detail: currentChoiceLabel(entry),
      keywords: entry.keywords,
      icon: <FlagIcon aria-hidden className="size-4 text-muted-foreground" />,
      opensPage: true,
      value: { kind: "flag", settingId: entry.id },
    }));
  }

  const handleAccept = (item: QuickPickItem<PickerValue>) => {
    const value = item.value;
    if (value.kind === "clear-overrides") {
      if (clearUiFeatureOverrides()) location.reload();
      else setStatus("Couldn't clear the flag overrides.");
    } else if (value.kind === "flag") {
      const chosen = settings.find((entry) => entry.id === value.settingId);
      if (chosen) {
        setOpened(chosen.id);
        setStatus(null);
        pages.push("choices", choiceItemId(chosen.id, chosen.current));
      }
    } else if (value.kind === "setting-choice" && setting) {
      const choice = value.value;
      if (choice !== "automatic" && choice !== "on" && choice !== "off") return "keepOpen" as const;
      if (choice === setting.current) {
        backToFlags();
        return "keepOpen" as const;
      }
      if (writeUiFeatureOverride(setting.id, choice === "automatic" ? null : choice === "on")) {
        location.reload();
      } else {
        setStatus(settingFailedStatus(setting));
      }
    }
    return "keepOpen" as const;
  };

  if (replaced) return null;
  return (
    <QuickPickDialog<PickerValue>
      stepsWithCtrlNP
      open={open && available}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) closeFeatureFlagsPicker();
      }}
      title="Override UI feature flag"
      description="Choose Automatic to follow the server, or override a UI feature on this device."
      label={onChoices ? "Choose a value" : "UI feature flags"}
      placeholder={onChoices ? "Choose a value…" : "Find a feature…"}
      backLabel="Back to features"
      heading={setting ? { label: setting.label } : undefined}
      segments={[
        { sourceId: "flags", ranking: "client", items },
        {
          sourceId: "actions",
          ranking: "client",
          items:
            !onChoices && activeUiFeatureOverrides(info).length > 0
              ? [
                  {
                    id: "clear-overrides",
                    label: "Clear all flag overrides",
                    icon: <RotateCcwIcon aria-hidden className="size-4 text-muted-foreground" />,
                    rememberInHistory: false,
                    value: { kind: "clear-overrides" },
                  },
                ]
              : [],
        },
      ]}
      {...pages.pickerProps}
      onBack={onChoices ? backToFlags : undefined}
      summary={status ? pageStatusSummary(status) : undefined}
      emptyMessage={onChoices ? "No matching values." : "No matching features."}
      acceptLabel={onChoices ? "Choose" : "Open"}
      onAccept={handleAccept}
    />
  );
}
