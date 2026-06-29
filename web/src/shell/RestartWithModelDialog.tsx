import { useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import { InfoIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { forkSession } from "@/lib/sessionsApi";

// Conservative model-id charset, kept in sync with the server's
// `omnigent.model_override._MODEL_ID_RE`: a leading alphanumeric (so the
// value can never read as a CLI flag) then dots / underscores / colons /
// slashes / brackets / dashes. Catches obvious typos client-side; the
// server re-validates and family-checks regardless.
const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/[\]-]*$/;

/**
 * Compact, codex-only "Restart with model…" dialog.
 *
 * Codex applies its model at launch, not mid-turn — there is no in-flight
 * model switch. So "restarting on a different model" is a fork that carries
 * the conversation history: this dialog drives the SAME
 * ``POST /v1/sessions/{id}/fork`` path the Clone dialog uses (the server
 * deep-copies the transcript and a codex-native target rebuilds its native
 * transcript), passing an explicit ``model_override`` so the clone launches
 * on the chosen model. The original session is untouched.
 *
 * Deliberately minimal (Option 1): a single model-id field + honest copy.
 * Not the full sidebar kebab menu. The model is a free-text id (e.g.
 * ``databricks-gpt-5-4-mini``) validated against the shared model-id charset;
 * the server is the authority on whether the id is routable for codex.
 *
 * @param sessionId - The codex-native session to restart.
 * @param currentModel - The session's current model override, prefilled into
 *   the field (so the user edits rather than retypes). ``null`` starts empty.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 */
export function RestartWithModelDialog({
  sessionId,
  currentModel,
  open,
  onOpenChange,
}: {
  sessionId: string;
  currentModel?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [model, setModel] = useState(currentModel ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = model.trim();
  // Enable submit only for a non-empty, charset-valid, *different* model —
  // restarting on the identical model is a no-op fork the user didn't mean.
  const canSubmit =
    trimmed !== "" && MODEL_ID_RE.test(trimmed) && trimmed !== (currentModel ?? "").trim();

  async function handleRestart(): Promise<void> {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      // Reuse the fork carry-history path with an explicit model override —
      // NOT a new restart mechanism. omit title/agent so the server keeps
      // the source's agent and derives "Fork of <title>".
      const fork = await forkSession(sessionId, undefined, undefined, undefined, trimmed);
      // Fire-and-forget: the sidebar refresh must not gate navigation.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onOpenChange(false);
      navigate(`/c/${fork.id}`);
    } catch (e) {
      // Nothing was created — leave the field editable for a resubmit. The
      // server's validation / family-mismatch error surfaces here verbatim.
      setError(e instanceof Error ? e.message : "Couldn't restart on that model. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="restart-model-dialog" className="flex flex-col gap-4 sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Restart with model…</DialogTitle>
          <DialogDescription>
            Starts a new session on the chosen model, carrying this conversation's history. The
            model applies at launch — Codex can't switch model mid-turn. Your current session is
            left untouched.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="restart-model-input"
            className="text-xs font-medium text-muted-foreground"
          >
            Model
          </label>
          <Input
            id="restart-model-input"
            data-testid="restart-model-input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !submitting && canSubmit) handleRestart();
            }}
            placeholder="databricks-gpt-5-4-mini"
            autoFocus
            className="font-mono text-xs"
          />
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <InfoIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>Enter a Codex (GPT) model id. The original session keeps its model.</span>
          </p>
        </div>

        {error !== null && (
          <p data-testid="restart-model-error" className="text-xs text-destructive">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="restart-model-submit"
            onClick={handleRestart}
            disabled={submitting || !canSubmit}
          >
            {submitting ? "Restarting…" : "Restart"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
