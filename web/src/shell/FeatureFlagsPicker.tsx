import { FlagIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { type QuickPickItem, QuickPickDialog, useQuickPickPages } from "@/components/ui/quick-pick";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import {
  canOverrideUiFeatures,
  OVERRIDABLE_UI_FEATURES,
  readUiFeatureOverrides,
  writeUiFeatureOverride,
} from "@/lib/uiFeatureOverrides";
import { closeFeatureFlagsPicker, useFeatureFlagsPickerStore } from "@/store/featureFlagsPicker";
import { currentChoiceLabel, type PaletteSetting } from "@/store/paletteSettings";

import type { PaletteTarget } from "./paletteTarget";
import { settingFailedStatus } from "./settingChoiceStatus";
import { choiceItemId, settingChoiceItems } from "./settingChoicesPage";

type PickerValue = { kind: "flag"; settingId: string } | PaletteTarget;

function statusSummary(status: string) {
  return function PageStatus() {
    return (
      <span role="status" data-slot="palette-page-status">
        {status}
      </span>
    );
  };
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

  const settings = useMemo<PaletteSetting[]>(() => {
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
        current: override === undefined ? "automatic" : override ? "on" : "off",
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
  useEffect(() => {
    if (onChoices && !setting) {
      setOpened(null);
      setStatus(null);
      pop();
    }
  }, [onChoices, setting, pop]);

  const items: QuickPickItem<PickerValue>[] = onChoices
    ? setting
      ? settingChoiceItems(setting.id, setting)
      : []
    : settings.map((entry) => ({
        id: entry.id,
        label: entry.label,
        detail: currentChoiceLabel(entry),
        keywords: entry.keywords,
        icon: <FlagIcon aria-hidden className="size-4 text-muted-foreground" />,
        opensPage: true,
        value: { kind: "flag", settingId: entry.id },
      }));

  const handleAccept = (item: QuickPickItem<PickerValue>) => {
    const value = item.value;
    if (value.kind === "flag") {
      const chosen = settings.find((entry) => entry.id === value.settingId);
      if (chosen) {
        setOpened(chosen.id);
        setStatus(null);
        pages.push("choices", choiceItemId(chosen.id, chosen.current));
      }
    } else if (value.kind === "setting-choice" && setting) {
      const chosen = OVERRIDABLE_UI_FEATURES.find((entry) => entry.feature === setting.id);
      if (!chosen) return "keepOpen" as const;
      const choice = value.value;
      if (choice !== "automatic" && choice !== "on" && choice !== "off") return "keepOpen" as const;
      if (writeUiFeatureOverride(chosen.feature, choice === "automatic" ? null : choice === "on")) {
        window.location.reload();
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
      segments={[{ sourceId: "flags", ranking: "client", items }]}
      {...pages.pickerProps}
      onBack={
        onChoices
          ? () => {
              setStatus(null);
              setOpened(null);
              pop();
            }
          : undefined
      }
      summary={status ? statusSummary(status) : undefined}
      emptyMessage={onChoices ? "No matching values." : "No matching features."}
      acceptLabel={onChoices ? "Choose" : "Open"}
      onAccept={handleAccept}
    />
  );
}
