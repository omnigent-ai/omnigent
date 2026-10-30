import { useEffect, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { AgentCard } from "@/components/AgentCard";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { childSessionsQueryKey } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { useHostModelOptions } from "@/hooks/useHosts";
import type { Session } from "@/lib/types";
import { setSessionDraft } from "@/lib/sessionDrafts";
import { createSession } from "@/lib/sessionsApi";

// Title sentinel marking a user-added agent. Mirrors the server's
// ``_UI_ADDED_AGENT_TITLE_PREFIX`` in omnigent/server/routes/sessions.py:
// the 3-segment "ui:<agent>:<name>" title is parsed back into
// tool=<agent>, session_name=<name> by the child_sessions endpoint, and a
// sub-agent named "ui" is rejected by the spec validator to avoid collision.
const UI_ADDED_TITLE_PREFIX = "ui";

/**
 * "Add agent" picker for the Agents rail.
 *
 * Lets the user attach a new agent (Claude Code, codex, a registered
 * custom agent) as a child of the active session. On submit it creates a
 * child session via ``POST /v1/sessions`` with ``parent_session_id`` set
 * and ``sub_agent_name`` left null (so the runner resolves the child's own
 * bound agent rather than a parent sub-spec), then navigates into the new
 * child so the user can send its first message. The agent catalog is the
 * same ``GET /v1/agents`` list the new-session picker uses.
 *
 * @param parentSessionId - Session the new agent is added under.
 * @param open - Whether the dialog is visible.
 * @param onOpenChange - Visibility setter (Radix-controlled).
 */
export function AddAgentDialog({
  parentSessionId,
  rootSessionId = parentSessionId,
  relatedSession = false,
  open,
  onOpenChange,
}: {
  parentSessionId: string;
  rootSessionId?: string;
  relatedSession?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: agents } = useAvailableAgents();
  const { session: sourceSession } = useSession(relatedSession ? parentSessionId : null);

  const agentList = agents ?? [];
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [firstMessage, setFirstMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !relatedSession || selectedAgentId || !sourceSession) return;
    const sourceAgent =
      agents?.find((agent) => agent.id === sourceSession.agentId) ??
      agents?.find((agent) => sourceSession.harness && agent.harness === sourceSession.harness);
    if (sourceAgent) setSelectedAgentId(sourceAgent.id);
  }, [open, relatedSession, selectedAgentId, sourceSession, agents]);

  const selectedAgent = agentList.find((a) => a.id === selectedAgentId) ?? null;

  function selectAgent(agentId: string): void {
    setSelectedAgentId(agentId);
    setModel("");
    setError(null);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedAgentId(null);
      setName("");
      setModel("");
      setFirstMessage("");
      setError(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  async function handleAdd(): Promise<void> {
    if (selectedAgent === null) return;
    const trimmed = name.trim() || (relatedSession ? selectedAgent.display_name : "");
    if (!trimmed) {
      setError("Enter a name for the agent.");
      return;
    }
    const title = relatedSession
      ? trimmed
      : `${UI_ADDED_TITLE_PREFIX}:${selectedAgent.name}:${trimmed}`;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(
        selectedAgent.id,
        [],
        relatedSession
          ? {
              relatedToSessionId: parentSessionId,
              title,
              ...(model ? { modelOverride: model } : {}),
            }
          : {
              parentSessionId,
              subAgentName: null,
              title,
            },
      );
      if (relatedSession && firstMessage.trim()) {
        setSessionDraft(session.id, { text: firstMessage, files: [] });
      }
      // Refresh the rail so the new child appears immediately, then jump
      // into it for the first message.
      await queryClient.invalidateQueries({
        queryKey: childSessionsQueryKey(rootSessionId),
      });
      handleOpenChange(false);
      navigate(`/c/${session.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't add the agent. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="add-agent-dialog"
        // max-w-lg matches NewChatDialog so the shared AgentCard renders at
        // the same width — and thus the same height once a description wraps.
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>{relatedSession ? "New session" : "Add agent"}</DialogTitle>
        </DialogHeader>

        {/* px-1/-mx-1 give the fields' 3px focus ring room to paint:
            overflow-y-auto also clips horizontally at the padding box. */}
        <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1">
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted-foreground">
              {relatedSession ? "Harness / agent" : "Pick an agent"}
            </span>
            {agentList.length === 0 ? (
              <p data-testid="add-agent-empty" className="text-sm text-muted-foreground">
                No agents available on this server. Register one with{" "}
                <code className="font-mono">omnigent server --agent</code>.
              </p>
            ) : (
              agentList.map((agent) => (
                <AgentCard
                  key={agent.id}
                  agent={agent}
                  selected={agent.id === selectedAgentId}
                  onSelect={() => selectAgent(agent.id)}
                  hover
                />
              ))
            )}
          </div>

          {selectedAgent !== null && (
            <div className="flex flex-col gap-1.5">
              <label htmlFor="add-agent-name" className="text-sm font-medium text-muted-foreground">
                Name
              </label>
              {/* Raw input matching NewChatDialog's "Name" field for a
                  consistent look (rounded-md + border-tint focus, no
                  heavy ring). */}
              <input
                id="add-agent-name"
                data-testid="add-agent-name-input"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={relatedSession ? selectedAgent.display_name : "Name this agent"}
                className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
              />
            </div>
          )}

          {relatedSession && (
            <>
              <RelatedSessionOptions
                sourceSession={sourceSession}
                harness={selectedAgent?.harness ?? null}
                model={model}
                onModelChange={setModel}
              />
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="add-agent-first-message"
                  className="text-sm font-medium text-muted-foreground"
                >
                  First message (optional draft)
                </label>
                <textarea
                  id="add-agent-first-message"
                  value={firstMessage}
                  onChange={(event) => setFirstMessage(event.target.value)}
                  placeholder="What should this agent work on?"
                  rows={3}
                  className="rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <p className="text-xs text-muted-foreground">
                  Review and send this message in the new session.
                </p>
              </div>
            </>
          )}
          {error !== null && (
            <p data-testid="add-agent-error" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="add-agent-submit"
            onClick={handleAdd}
            loading={submitting}
            disabled={selectedAgent === null || (!relatedSession && !name.trim())}
          >
            {relatedSession ? "Create session" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RelatedSessionOptions({
  sourceSession,
  harness,
  model,
  onModelChange,
}: {
  sourceSession: Session | null;
  harness: string | null;
  model: string;
  onModelChange: (model: string) => void;
}) {
  const { data: models } = useHostModelOptions(
    sourceSession?.hostId ?? null,
    harness ?? "",
    Boolean(harness),
    { poll: false },
  );
  return (
    <div className="grid grid-cols-2 gap-3">
      <label className="col-span-2 flex flex-col gap-1.5 text-sm font-medium text-muted-foreground">
        Model
        <select
          aria-label="Model"
          value={model}
          onChange={(event) => onModelChange(event.target.value)}
          className="rounded-md border border-input bg-background px-3 py-2 text-foreground"
        >
          <option value="">Harness default</option>
          {(models ?? []).map((option) => (
            <option key={option.id} value={option.id}>
              {option.displayName ?? option.id}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium text-muted-foreground">
        Context
        <select
          aria-label="Context"
          defaultValue="fresh"
          className="rounded-md border border-input bg-background px-3 py-2 text-foreground"
        >
          <option value="fresh">Fresh context</option>
          <option disabled>Fork context — coming soon</option>
        </select>
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium text-muted-foreground">
        Environment
        <select
          aria-label="Environment"
          defaultValue="same"
          className="rounded-md border border-input bg-background px-3 py-2 text-foreground"
        >
          <option value="same">Same as current session</option>
          <option disabled>New sandbox — coming soon</option>
        </select>
      </label>
      <p className="col-span-2 text-xs text-muted-foreground">
        Uses the current session’s environment and workspace. More context and placement options are
        planned.
      </p>
    </div>
  );
}
