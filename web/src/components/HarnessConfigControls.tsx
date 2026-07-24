import { type ReactNode, useState } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Sentinel Select values for the Model row. Radix requires a non-empty value,
// so the two "no explicit model" choices ride on reserved tokens rather than
// "": DEFAULT = the harness's own configured model (no override), SMART = the
// intelligent router picks per turn.
export const MODEL_SELECT_DEFAULT = "__default__";
export const MODEL_SELECT_SMART = "__smart__";
// Sentinel for the "no explicit effort" (—) choice, same reasoning.
export const EFFORT_SELECT_NONE = "__none__";

/**
 * A labeled configuration row: bold label + muted sub-description on the left,
 * the control on the right. Mirrors the "Configure …" modal layout.
 */
export function ConfigRow({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    // Stacked on mobile (label above a full-width control) so the label never
    // gets squeezed into a narrow column and wraps hard; side-by-side from sm+
    // with the control pinned to a fixed width.
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="min-w-0 sm:pt-1">
        <div className="text-sm font-medium">{label}</div>
        {description && <div className="text-xs text-muted-foreground">{description}</div>}
      </div>
      <div className="w-full sm:w-52 sm:shrink-0">{children}</div>
    </div>
  );
}

/**
 * A config-modal Select whose options carry descriptions. The description of
 * the hovered / focused option (falling back to the selected one) shows in a
 * footer line pinned at the bottom of the OPEN dropdown. The popup is pinned to
 * the trigger width and the footer wraps, so the dropdown never changes width
 * as you hover across options.
 *
 * @param value Selected option value.
 * @param onValueChange Selection callback.
 * @param options Value/label/description triples.
 * @param testId Trigger test id.
 * @param ariaLabel Accessible name for the trigger (the visible ConfigRow
 *   label is visual-only, so pass it here to name the control for AT).
 */
export function DescribedSelect({
  value,
  onValueChange,
  options,
  testId,
  ariaLabel,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: readonly { value: string; label: string; description: string }[];
  testId: string;
  ariaLabel: string;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = options.find((o) => o.value === (previewed ?? value))?.description;
  return (
    <Select
      value={value}
      onValueChange={onValueChange}
      // Reset the preview when the list closes so the next open starts on the
      // selected option's blurb.
      onOpenChange={(next) => {
        if (!next) setPreviewed(null);
      }}
    >
      <SelectTrigger className="w-full" data-testid={testId} aria-label={ariaLabel}>
        <SelectValue />
      </SelectTrigger>
      {/* Pin the popup to the trigger width so a long blurb wraps in the footer
      instead of widening the list as you hover across options. */}
      <SelectContent
        position="popper"
        align="start"
        className="w-(--radix-select-trigger-width) [&_[data-slot=select-item]]:pl-2.5"
      >
        {options.map((o) => (
          <SelectItem
            key={o.value}
            value={o.value}
            onPointerEnter={() => setPreviewed(o.value)}
            onFocus={() => setPreviewed(o.value)}
          >
            {o.label}
          </SelectItem>
        ))}
        {/* Footer blurb pinned inside the dropdown, tracking the hovered row.
        min-h reserves a line so the popup height doesn't jump as it changes. */}
        <SelectSeparator />
        <p
          data-testid={`${testId}-detail`}
          className="min-h-8 px-2.5 pt-0.5 pb-1 text-xs leading-snug text-muted-foreground"
        >
          {detail}
        </p>
      </SelectContent>
    </Select>
  );
}
