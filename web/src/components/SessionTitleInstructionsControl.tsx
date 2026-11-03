import { useCallback, useEffect, useId, useState } from "react";
import { toast } from "sonner";

import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  getSessionTitleInstructions,
  saveSessionTitleInstructions,
} from "@/lib/sessionTitleInstructionsApi";
import { cn } from "@/lib/utils";

// Matches the server limit until the first read reports it.
const DEFAULT_MAX_CHARS = 4000;

type LoadState = "loading" | "ready" | "error";

/** Edits the signed-in user's saved guidance for automatic session titles. */
export function SessionTitleInstructionsControl() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [saved, setSaved] = useState("");
  const [draft, setDraft] = useState("");
  const [maxChars, setMaxChars] = useState(DEFAULT_MAX_CHARS);
  const [saving, setSaving] = useState(false);
  const labelId = useId();
  const descriptionId = useId();
  const counterId = useId();

  const load = useCallback(async (isCurrent: () => boolean) => {
    setLoadState("loading");
    try {
      const current = await getSessionTitleInstructions();
      if (!isCurrent()) return;
      setSaved(current.instructions ?? "");
      setDraft(current.instructions ?? "");
      setMaxChars(current.max_chars);
      setLoadState("ready");
    } catch {
      if (isCurrent()) setLoadState("error");
    }
  }, []);

  useEffect(() => {
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  const persist = useCallback(async (next: string | null) => {
    setSaving(true);
    try {
      const result = await saveSessionTitleInstructions(next);
      setSaved(result.instructions ?? "");
      setDraft(result.instructions ?? "");
      toast.success(
        result.instructions ? "Title instructions saved" : "Title instructions cleared",
      );
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      toast.error(`Couldn't save title instructions: ${reason}`);
    } finally {
      setSaving(false);
    }
  }, []);

  const trimmed = draft.trim();
  const tooLong = trimmed.length > maxChars;
  const ready = loadState === "ready";
  const dirty = ready && trimmed !== saved;

  return (
    <div className="flex flex-col gap-3" data-testid="session-title-instructions-control">
      <SettingsLabel
        label="Title instructions"
        labelId={labelId}
        descriptionId={descriptionId}
        description="Guidance for automatic session titles, such as a format, language or prefix. Leave blank to use the default title prompt."
        descriptionClassName="text-ui"
      />
      <Textarea
        aria-labelledby={labelId}
        aria-describedby={`${descriptionId} ${counterId}`}
        aria-invalid={tooLong || undefined}
        data-testid="session-title-instructions-input"
        placeholder="e.g. Start every title with the ticket id, like OMNI-123."
        rows={4}
        className="min-h-24 max-h-64"
        value={draft}
        disabled={!ready || saving}
        onChange={(event) => setDraft(event.target.value)}
        componentId="settings.general.session_title_instructions"
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span
          id={counterId}
          data-testid="session-title-instructions-status"
          className={cn("text-sm text-muted-foreground", tooLong && "text-destructive")}
        >
          {loadState === "loading" && "Loading…"}
          {loadState === "error" && "Couldn't load your title instructions."}
          {ready &&
            (tooLong
              ? `${trimmed.length} / ${maxChars} characters — shorten to save`
              : `${trimmed.length} / ${maxChars} characters`)}
        </span>
        <div className="flex items-center gap-2">
          {loadState === "error" && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => void load(() => true)}
              data-testid="session-title-instructions-retry"
              componentId="settings.general.session_title_instructions.retry"
            >
              Retry
            </Button>
          )}
          {ready && saved && (
            <Button
              variant="ghost"
              size="sm"
              disabled={saving}
              onClick={() => void persist(null)}
              data-testid="session-title-instructions-clear"
              componentId="settings.general.session_title_instructions.clear"
            >
              Clear
            </Button>
          )}
          {ready && (
            <Button
              size="sm"
              disabled={!dirty || tooLong || saving}
              onClick={() => void persist(trimmed || null)}
              data-testid="session-title-instructions-save"
              componentId="settings.general.session_title_instructions.save"
            >
              {saving ? "Saving…" : "Save"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
