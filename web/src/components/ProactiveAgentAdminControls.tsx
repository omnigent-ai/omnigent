import { useEffect, useId, useState } from "react";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  useProactiveAgentPrompt,
  useRunProactiveAgent,
  type ProactiveAgentRuns,
} from "@/hooks/useProactiveAgentSettings";
import { PROACTIVE_AGENT_DISPLAY_NAME } from "@/lib/proactiveInbox";

const SKIP_REASONS: Record<ProactiveAgentRuns["skipped"][number]["reason"], string> = {
  no_task: "no task yet",
  turned_off: "turned off",
  already_running: "already running",
  unavailable: "scheduler unavailable",
};

/** Split the Run now field into user ids; a lone "*" means everyone. */
export function parseRunUsers(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).filter(Boolean))];
}

function runSummary(result: ProactiveAgentRuns): string {
  const started = `Started ${result.started.length} ${result.started.length === 1 ? "run" : "runs"}`;
  const skipped = result.skipped.map(
    ({ user_id, reason }) => `${user_id} (${SKIP_REASONS[reason]})`,
  );
  const parts = [started + (result.truncated ? ", the most one request starts" : "") + "."];
  if (skipped.length) parts.push(`Skipped: ${skipped.join(", ")}.`);
  return parts.join(" ");
}

/**
 * Admin controls for iterating on Omni Advisor: replace its prompt for the
 * workspace, and start runs now for chosen users. Render only for admins.
 */
export function ProactiveAgentAdminControls() {
  const { query, save } = useProactiveAgentPrompt(true);
  const run = useRunProactiveAgent();
  const [draft, setDraft] = useState<string | null>(null);
  const [users, setUsers] = useState("");
  const promptId = useId();
  const usersId = useId();
  const current = query.data;
  // Start from the saved prompt; keep edits until they are saved or reset.
  useEffect(() => {
    if (current && draft === null) setDraft(current.prompt);
  }, [current, draft]);
  const runUsers = parseRunUsers(users);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <SettingsLabel
          label="Prompt"
          labelId={promptId}
          description={`Replaces ${PROACTIVE_AGENT_DISPLAY_NAME}'s built-in instructions for every user, starting with the next run. Restoring the default follows the built-in prompt again as releases update it.`}
        />
        {query.isError && (
          <p role="alert" className="text-ui text-destructive">
            {query.error.message}
          </p>
        )}
        {current && draft !== null && (
          <>
            <Textarea
              aria-labelledby={promptId}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={14}
              className="max-h-[32rem] font-mono text-sm"
              componentId="settings.proactive_agent.prompt"
            />
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                disabled={save.isPending || draft.trim() === "" || draft === current.prompt}
                onClick={() => save.mutate(draft, { onSuccess: (data) => setDraft(data.prompt) })}
                componentId="settings.proactive_agent.prompt_save"
              >
                Save prompt
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={save.isPending || !current.overridden}
                onClick={() => save.mutate(null, { onSuccess: (data) => setDraft(data.prompt) })}
                componentId="settings.proactive_agent.prompt_reset"
              >
                Restore default
              </Button>
              <span className="text-sm text-muted-foreground">
                {current.overridden ? "Using a custom prompt" : "Using the built-in prompt"}
              </span>
            </div>
            {save.isError && (
              <p role="alert" className="text-ui text-destructive">
                {save.error.message}
              </p>
            )}
          </>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <SettingsLabel
          label="Run now"
          labelId={usersId}
          description={`Start runs right away for these users, separated by commas or spaces, or * for everyone who has ${PROACTIVE_AGENT_DISPLAY_NAME} on. Users who have not started a session since it was enabled have no run to start yet.`}
        />
        <div className="flex items-center gap-2">
          <Input
            aria-labelledby={usersId}
            value={users}
            onChange={(event) => setUsers(event.target.value)}
            placeholder="alice@example.com, bob@example.com or *"
            className="flex-1"
            componentId="settings.proactive_agent.run_users"
          />
          <Button
            size="sm"
            disabled={run.isPending || runUsers.length === 0}
            onClick={() => run.mutate(runUsers)}
            componentId="settings.proactive_agent.run_now"
          >
            Run now
          </Button>
        </div>
        {run.data && <p className="text-sm text-muted-foreground">{runSummary(run.data)}</p>}
        {run.isError && (
          <p role="alert" className="text-ui text-destructive">
            {run.error.message}
          </p>
        )}
      </div>
    </div>
  );
}
