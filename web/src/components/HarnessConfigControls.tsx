import { type ReactNode, useState } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";

// Sentinel Select values for the Model row. Radix requires a non-empty value,
// so the two "no explicit model" choices ride on reserved tokens rather than
// "": DEFAULT = the harness's own configured model (no override), SMART = the
// intelligent router picks per turn.
export const MODEL_SELECT_DEFAULT = "__default__";
export const MODEL_SELECT_SMART = "__smart__";
// Sentinel for the "no explicit effort" (—) choice, same reasoning.
export const EFFORT_SELECT_NONE = "__none__";
// Shown in the frozen Effort row when the router picks the model per turn, so
// no effort can apply. Rendered as the Select's placeholder (value "").
export const EFFORT_UNAVAILABLE_PLACEHOLDER = "—";

/** One entry in the Model row's harness-model list. */
export interface RoutingModelOption {
  id: string;
  label: string;
}

/**
 * The Model row's Select: the Smart Routing sentinel (when offered), the
 * harness's own "Default", then the harness's models. Shared by the landing
 * dialog and the in-session composer so the sentinels and their copy can't
 * drift across the three places the row appears.
 *
 * @param value Selected value — a model id or one of the sentinels.
 * @param onValueChange Selection callback.
 * @param offerSmartRouting Whether to list the Smart Routing sentinel. False on
 *   harnesses/sessions the router can't route.
 * @param testId Trigger test id.
 * @param ariaLabel Accessible name for the trigger (the visible ConfigRow label
 *   is visual-only, so pass it here to name the control for AT).
 * @param models Harness models, listed after "Default". Empty when the harness
 *   resolves its own catalog and only the two sentinels are expressible.
 * @param defaultLabel Label for the "no explicit model" row. Defaults to
 *   "Default"; pass the resolved form (e.g. `Default (gpt-5.5)`) where the
 *   catalog names which model that is.
 * @param activeModelId Model marked `data-active` in the list, when any.
 * @param contentClassName Extra classes for the dropdown popup.
 * @param children Rendered below the model list, e.g. a loading/empty note.
 */
export function RoutingModelSelect({
  value,
  onValueChange,
  offerSmartRouting,
  testId,
  ariaLabel = "Model",
  models,
  defaultLabel = "Default",
  activeModelId,
  contentClassName,
  children,
}: {
  value: string;
  onValueChange: (value: string) => void;
  offerSmartRouting: boolean;
  testId: string;
  ariaLabel?: string;
  models?: readonly RoutingModelOption[];
  defaultLabel?: string;
  activeModelId?: string | null;
  contentClassName?: string;
  children?: ReactNode;
}) {
  return (
    <Select value={value} onValueChange={onValueChange}>
      <SelectTrigger className="w-full" data-testid={testId} aria-label={ariaLabel}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className={contentClassName}>
        {offerSmartRouting && (
          <SelectItem value={MODEL_SELECT_SMART}>{SMART_ROUTING_LABEL}</SelectItem>
        )}
        <SelectItem value={MODEL_SELECT_DEFAULT}>{defaultLabel}</SelectItem>
        {(models ?? []).map((m) => (
          <SelectItem
            key={m.id}
            value={m.id}
            data-model-id={m.id}
            data-active={activeModelId === m.id ? "true" : undefined}
          >
            {m.label}
          </SelectItem>
        ))}
        {children}
      </SelectContent>
    </Select>
  );
}

// Claude-native reasoning-effort options for the new-session / scheduled-task
// model+effort pickers. There is deliberately no hardcoded effort default: an
// unselected picker omits `reasoning_effort`, so Claude Code falls back to its
// own configured effort — the same "no override" semantics the in-session
// picker's `null` state uses. Mirrors ANTHROPIC_EFFORTS server-side. Lives here
// (a leaf module, no heavy imports) so both NewChatDialog and the scheduled-task
// dialog can share the single source of truth.
export const CLAUDE_NATIVE_EFFORTS: { value: string; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "xHigh" },
  { value: "max", label: "Max" },
];

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
        <div className="text-ui font-medium">{label}</div>
        {description && <div className="text-sm text-muted-foreground">{description}</div>}
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
 * @param disabled Locks the control: the value shows but the list can't open.
 *   Used for a row whose value isn't the user's to choose (yet).
 */
export function DescribedSelect({
  value,
  onValueChange,
  options,
  testId,
  ariaLabel,
  disabled,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: readonly { value: string; label: string; description: string }[];
  testId: string;
  ariaLabel: string;
  disabled?: boolean;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = options.find((o) => o.value === (previewed ?? value))?.description;
  return (
    <Select
      value={value}
      onValueChange={onValueChange}
      disabled={disabled}
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
          className="min-h-8 px-2.5 pt-0.5 pb-1 text-sm leading-snug text-muted-foreground"
        >
          {detail}
        </p>
      </SelectContent>
    </Select>
  );
}
