import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { switchSessionAgent } from "@/lib/sessionsApi";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";
import { agentRootName, forkTargetCarriesHistory, harnessFamily } from "@/lib/forkHarness";

// "" means no target chosen yet. It must be empty (not a sentinel like
// "__none__"): Radix only renders the trigger placeholder when the controlled
// value is empty/undefined — a non-empty value that matches no <SelectItem>
// renders a BLANK trigger instead. We pass ``value || undefined`` below for
// the same reason, and keep the submit button disabled while it's "".
const NONE_CHOSEN = "";

/**
 * Switch an open session in place to a different agent/harness.
 *
 * NOTE: currently unmounted — the header's "Switch agent" button was
 * removed, but the dialog (and its `/v1/sessions/{id}/switch-agent`
 * plumbing) is kept, tested, for when the affordance returns.
 *
 * Unlike Clone (fork), this keeps the SAME session — transcript, comments,
 * files, and workspace are untouched; only the agent/harness changes, and
 * the next turn runs on it. The picker lists only history-preserving targets
 * (the same rule as the fork agent picker, via `forkTargetCarriesHistory`).
 * A cross-family switch resets model + reasoning effort to the target's
 * defaults, which the dialog warns about before submitting. On success it
 * refreshes the bound-agent and session-list queries; a failure (e.g. 409
 * while a turn is running) stays open and surfaces the error inline.
 *
 * @param sessionId - The session to switch.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 */
export function SwitchAgentDialog({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [agentChoice, setAgentChoice] = useState<string>(NONE_CHOSEN);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Built-in switch targets + the session's currently-bound agent (for its
  // harness, so we offer only history-preserving targets and warn when a
  // cross-family switch resets model settings). Only fetched while open.
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: currentAgent } = useSessionAgent(open ? sessionId : null);

  // The bound agent stripped of EVERY " (fork <id>)" / " (switch <id>)"
  // suffix the fork/switch routes append when cloning (a fork of a fork
  // nests them), so a clone of a built-in — however deep — still matches
  // that built-in by name and is excluded below.
  const currentAgentName = currentAgent?.name ?? null;
  const currentAgentRootName = currentAgentName ? agentRootName(currentAgentName) : null;

  // Friendly label for the currently-bound agent, shown as the trigger's
  // default (greyed) so the box isn't blank and the user sees what they're on.
  // Prefer the matching built-in's display_name (e.g. "nessie" → "Nessie");
  // fall back to the bound agent's own (suffix-stripped) name.
  const currentDisplay =
    (agents ?? []).find((a) => a.name === currentAgentName || a.name === currentAgentRootName)
      ?.display_name ??
    currentAgentRootName ??
    currentAgentName;

  // Targets, excluding the session's own agent (switching to it is a no-op)
  // and any target that wouldn't preserve history — SDK and native targets
  // both carry from any source (see forkTargetCarriesHistory); only
  // unclassifiable harnesses are hidden.
  const switchableAgents = (agents ?? []).filter(
    (a) =>
      a.id !== currentAgent?.id &&
      a.name !== currentAgentName &&
      a.name !== currentAgentRootName &&
      forkTargetCarriesHistory(a.harness),
  );

  const chosen = switchableAgents.find((a) => a.id === agentChoice) ?? null;
  // A cross-family switch can't carry the provider-bound model id, so the
  // server resets model_override / reasoning_effort to the target's defaults.
  const resetsModelSettings =
    chosen !== null && harnessFamily(currentAgent?.harness) !== harnessFamily(chosen.harness);

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setAgentChoice(NONE_CHOSEN);
      setError(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  async function handleSwitch(): Promise<void> {
    if (agentChoice === NONE_CHOSEN) return;
    setSubmitting(true);
    setError(null);
    try {
      await switchSessionAgent(sessionId, agentChoice);
      // The bound agent changed; refresh it and the sidebar so the new
      // harness shows. Same session, so no navigation. The per-session
      // snapshot (["session", sessionId], staleTime: Infinity) carries the
      // model settings + presentation labels the switch just reset, so it
      // must be invalidated too or the UI keeps showing pre-switch fields
      // until a full reload.
      await queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      await queryClient.invalidateQueries({ queryKey: ["session-agent", sessionId] });
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      handleOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't switch the agent. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent data-testid="switch-agent-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Switch agent</DialogTitle>
          <DialogDescription>
            Continue this session on a different agent. The conversation, comments, and files stay;
            the next message runs on the new agent.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="switch-agent-select"
            className="text-xs font-medium text-muted-foreground"
          >
            Agent
          </label>
          <Select value={agentChoice || undefined} onValueChange={setAgentChoice}>
            <SelectTrigger
              id="switch-agent-select"
              data-testid="switch-agent-select"
              className="w-full text-xs"
            >
              <SelectValue
                placeholder={
                  currentDisplay ? (
                    <span data-testid="switch-agent-current">
                      <span className="text-foreground">{currentDisplay}</span>{" "}
                      <span className="text-muted-foreground">(current agent)</span>
                    </span>
                  ) : (
                    "Choose an agent"
                  )
                }
              />
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              {switchableAgents.map((agent) => (
                <SelectItem
                  key={agent.id}
                  value={agent.id}
                  data-testid={`switch-agent-option-${agent.id}`}
                  className="text-xs"
                >
                  {agent.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {resetsModelSettings && (
          <p data-testid="switch-agent-reset-warning" className="text-xs text-muted-foreground">
            Model &amp; reasoning effort will reset to {chosen?.display_name}'s defaults (different
            provider).
          </p>
        )}

        {error !== null && (
          <p data-testid="switch-agent-error" className="text-xs text-destructive">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="switch-agent-submit"
            onClick={handleSwitch}
            disabled={submitting || agentChoice === NONE_CHOSEN}
          >
            {submitting ? "Switching…" : "Switch"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
