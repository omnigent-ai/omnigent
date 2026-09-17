import type { ReactNode } from "react";
import { DropdownMenuCheckboxItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
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
      {choices.map((choice) => (
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
      ))}
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
  extra,
}: {
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
  // Additional sections rendered after Models/Effort — e.g. Devin Fusion's
  // Lead / Effort / Sidekick selectors. Each gets its own separator + header.
  extra?: readonly ComposerConfigSection[];
}) {
  const sections = [...(models ? [models] : []), ...(efforts ? [efforts] : []), ...(extra ?? [])];
  return (
    <>
      {sections.map((section, index) => (
        <div key={section.testId} data-testid={section.testId}>
          {index > 0 && <DropdownMenuSeparator />}
          <PickerSectionHeader>{section.header}</PickerSectionHeader>
          {section.leading}
          <ConfigChoices choices={section.choices} />
        </div>
      ))}
    </>
  );
}
