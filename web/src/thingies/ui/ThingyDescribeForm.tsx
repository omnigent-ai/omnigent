// Describing a new thingy: a description, suggestions to start from, and Build. The New thingy
// dialog, the create page and a session's New thingy tab show it, each building it its own way.
import { type ReactNode, useId, useState } from "react";

import { CompactShortcutKeys, ENTER_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ThingyErrorNotice } from "./ThingyErrorNotice";

export const THINGY_DESCRIPTION_PLACEHOLDER =
  "What it shows, what data it uses, and what people do with it";

export const THINGY_SUGGESTIONS = [
  "A weekly KPI story from a table",
  "An incident tracker for my team",
  "A launch checklist with owners",
  "A reading list with notes",
] as const;

export interface ThingyDescribeFormProps {
  /** Shown above the description; the dialog leaves it out, its title says it. */
  heading?: ReactNode;
  explanation: ReactNode;
  /** Builds the thingy from the description; a rejection shows under the form and keeps it. */
  onBuild: (description: string) => Promise<void>;
  /** Shows Cancel, which calls it. */
  onCancel?: () => void;
  errorTitle: string;
}

export function ThingyDescribeForm({
  heading,
  explanation,
  onBuild,
  onCancel,
  errorTitle,
}: ThingyDescribeFormProps) {
  const [description, setDescription] = useState("");
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
      aria-label={heading ? undefined : "Describe the thingy"}
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
        aria-label="Describe the thingy"
        aria-describedby={explanationId}
        // The form is the reason its dialog or tab opened.
        autoFocus
        rows={4}
        value={description}
        placeholder={THINGY_DESCRIPTION_PLACEHOLDER}
        disabled={building}
        componentId="thingies.describe.description"
        className="max-h-72 min-h-24 resize-none"
        onChange={(event) => setDescription(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void build();
          }
        }}
      />
      <div className="flex flex-wrap gap-1.5" aria-label="Suggestions">
        {THINGY_SUGGESTIONS.map((suggestion) => (
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
        <span className="flex min-w-0 flex-1 items-center gap-1 text-sm text-muted-foreground">
          <CompactShortcutKeys keys={[MOD_KEY, ENTER_KEY]} />
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
          componentId="thingies.describe.build"
          onClick={() => void build()}
        >
          Build
        </Button>
      </div>
      {error !== undefined && <ThingyErrorNotice title={errorTitle} error={error} />}
    </section>
  );
}
