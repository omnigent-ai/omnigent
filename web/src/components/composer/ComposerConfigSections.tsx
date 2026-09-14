import type { ReactNode } from "react";
import {
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  deriveModelProviders,
  partitionModelOptionsByProvider,
} from "@/components/HarnessConfigControls";
import { PickerSectionHeader } from "./HarnessMenuRow";

/** One checkbox row in a Models or Effort section. */
export interface ComposerConfigChoice {
  key: string;
  label: ReactNode;
  checked: boolean;
  disabled?: boolean;
  // Omitted for a static, non-selectable row (e.g. the disabled "(current)"
  // model). When present it drives the checkbox's change handler.
  onSelect?: () => void;
  testId?: string;
  title?: string;
  className?: string;
  // Extra data-* attributes (e.g. data-model-id / data-effort-level).
  data?: Record<string, string | undefined>;
  // When set, the row renders as a non-interactive group label (e.g. a
  // provider header interleaved into the model list) instead of a checkbox
  // item. Produced by {@link providerGroupedChoices}, not hand-written.
  sectionLabel?: string;
}

/** A single labeled section (Models or Effort) of the harness config menu. */
export interface ComposerConfigSection {
  testId: string;
  header: ReactNode;
  // Rendered between the header and the choices — e.g. a model search box or a
  // loading/empty note. Page-local because it varies per surface.
  leading?: ReactNode;
  choices: ComposerConfigChoice[];
}

function ConfigChoices({ choices }: { choices: readonly ComposerConfigChoice[] }) {
  return (
    <>
      {choices.map((choice) =>
        choice.sectionLabel !== undefined ? (
          <DropdownMenuLabel
            key={choice.key}
            data-provider={choice.sectionLabel}
            className={choice.className}
          >
            {choice.label}
          </DropdownMenuLabel>
        ) : (
          <DropdownMenuCheckboxItem
            key={choice.key}
            checked={choice.checked}
            disabled={choice.disabled}
            onSelect={(event) => event.preventDefault()}
            onCheckedChange={choice.onSelect ? () => choice.onSelect?.() : undefined}
            data-testid={choice.testId}
            title={choice.title}
            className={choice.className}
            {...choice.data}
          >
            {choice.label}
          </DropdownMenuCheckboxItem>
        ),
      )}
    </>
  );
}

/**
 * The shared Models + Effort menu sections rendered inside both harness pickers
 * — the in-session composer (ChatPage) and the landing dialog (NewChatDialog).
 *
 * Each page supplies the option data, labels, callbacks, and any search/loading
 * slot; the section structure (header, separator, checkbox rows) lives here so
 * the two surfaces render the same composed menu instead of drifting into
 * separate page-local copies. Pass a section as undefined to omit it.
 */
export function ComposerConfigSections({
  models,
  efforts,
}: {
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
}) {
  return (
    <>
      {models && (
        <div data-testid={models.testId}>
          <PickerSectionHeader>{models.header}</PickerSectionHeader>
          {models.leading}
          <ConfigChoices choices={models.choices} />
        </div>
      )}
      {efforts && (
        <div data-testid={efforts.testId}>
          <DropdownMenuSeparator />
          <PickerSectionHeader>{efforts.header}</PickerSectionHeader>
          {efforts.leading}
          <ConfigChoices choices={efforts.choices} />
        </div>
      )}
    </>
  );
}

// Provider group headers interleaved into a model choices list: muted label
// rows matching the visual weight of a section header, one indent step in.
const PROVIDER_LABEL_CLASS = "px-3 pt-1 text-[11px] font-normal text-muted-foreground/80";

/**
 * Build the model list as provider-grouped choices for a config section.
 *
 * Groups under provider label rows only when the WHOLE catalog spans two or
 * more providers; single-provider and provider-less catalogs keep the flat
 * list exactly as before the field existed. Options are never dropped —
 * provider-less ones trail in an unlabeled group — and a group emptied by
 * the caller's search filter simply renders no rows, so its label never
 * dangles. Shared by the composer agent-config menus so the two
 * implementations cannot drift.
 */
export function providerGroupedChoices<T extends { id: string; provider?: string }>(
  options: readonly T[],
  allOptions: readonly T[],
  toChoice: (option: T) => ComposerConfigChoice,
): ComposerConfigChoice[] {
  const grouped =
    deriveModelProviders(allOptions).length >= 2 && partitionModelOptionsByProvider(options);
  if (!grouped) return options.map(toChoice);
  return grouped.flatMap((section) => [
    ...(section.provider
      ? [
          {
            key: `__provider_${section.provider}`,
            label: section.provider,
            checked: false,
            className: PROVIDER_LABEL_CLASS,
            sectionLabel: section.provider,
          },
        ]
      : []),
    ...section.options.map(toChoice),
  ]);
}
