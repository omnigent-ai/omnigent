// Describing a new gadget: a description, suggestions to start from, and Build. The New gadget
// dialog, the create page and a session's New gadget tab show it, each building it its own way.
import { type ReactNode, useId, useState } from "react";

import {
  ARIA_MOD_KEY,
  CompactShortcutKeys,
  ENTER_KEY,
  MOD_KEY,
} from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { GadgetErrorNotice } from "./GadgetErrorNotice";

export const GADGET_DESCRIPTION_PLACEHOLDER =
  "e.g. A tracker of open incidents with owner and severity";

export const GADGET_SUGGESTIONS = [
  "A weekly KPI story from a table",
  "An incident tracker for my team",
  "A launch checklist with owners",
  "A reading list with notes",
] as const;

export interface GadgetDescribeFormProps {
  /** Shown above the description; the dialog leaves it out, its title says it. */
  heading?: ReactNode;
  explanation: ReactNode;
  /** Builds the gadget from the description; a rejection shows under the form and keeps it. */
  onBuild: (description: string) => Promise<void>;
  /** Shows Cancel, which calls it. */
  onCancel?: () => void;
  errorTitle: string;
  /** The description to start with, such as one typed before a reload. */
  initialDescription?: string;
  onDescriptionChange?: (description: string) => void;
  /** Caps how tall the description grows, where the form shares a dialog with the templates. */
  compact?: boolean;
}

export function GadgetDescribeForm({
  heading,
  explanation,
  onBuild,
  onCancel,
  errorTitle,
  initialDescription = "",
  onDescriptionChange,
  compact = false,
}: GadgetDescribeFormProps) {
  const [description, setDescriptionState] = useState(initialDescription);
  const setDescription = (next: string) => {
    setDescriptionState(next);
    onDescriptionChange?.(next);
  };
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<unknown>(undefined);
  const headingId = useId();
  const explanationId = useId();
  const build = async () => {
    const text = description.trim();
    if (!text || building) return;
    setBuilding(true);
    setError(undefined);
    try {
      await onBuild(text);
    } catch (buildError) {
      setError(buildError);
      setBuilding(false);
    }
  };
  return (
    <section
      aria-labelledby={heading ? headingId : undefined}
      aria-label={heading ? undefined : "Describe the gadget"}
      className="flex flex-col gap-3"
    >
      <div className="flex flex-col gap-1">
        {heading && (
          <h2 id={headingId} className="text-base font-semibold">
            {heading}
          </h2>
        )}
        <p id={explanationId} className="text-ui text-muted-foreground">
          {explanation}
        </p>
      </div>
      <Textarea
        aria-label="Describe the gadget"
        aria-describedby={explanationId}
        // The form is the reason its dialog or tab opened.
        autoFocus
        rows={4}
        value={description}
        placeholder={GADGET_DESCRIPTION_PLACEHOLDER}
        disabled={building}
        componentId="gadgets.describe.description"
        className={cn("min-h-24 resize-none", compact ? "max-h-48" : "max-h-72")}
        onChange={(event) => setDescription(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void build();
          }
        }}
      />
      <div className="flex flex-wrap gap-1.5" aria-label="Suggestions">
        {GADGET_SUGGESTIONS.map((suggestion) => (
          <Button
            key={suggestion}
            type="button"
            variant="outline"
            size="xs"
            className="rounded-full px-2.5 text-muted-foreground hover:text-foreground"
            disabled={building}
            onClick={() => setDescription(suggestion)}
          >
            {suggestion}
          </Button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        {/* A keyboard hint, so touch screens leave it out. */}
        <span className="flex min-w-0 flex-1 items-center gap-1 text-sm text-muted-foreground max-md:invisible pointer-coarse:invisible">
          <CompactShortcutKeys keys={[MOD_KEY, ENTER_KEY]} />
          <span className="sr-only">{ARIA_MOD_KEY === "Meta" ? "Command" : "Control"}+Enter</span>
          to build
        </span>
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button
          type="button"
          loading={building}
          disabled={!description.trim()}
          componentId="gadgets.describe.build"
          onClick={() => void build()}
        >
          Build
        </Button>
      </div>
      {error !== undefined && <GadgetErrorNotice title={errorTitle} error={error} />}
    </section>
  );
}
