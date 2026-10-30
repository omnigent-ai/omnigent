import { useMemo, useState } from "react";
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
import { createSession } from "@/lib/sessionsApi";
import { useSession } from "@/hooks/useSession";
import { Spinner } from "@/components/ui/spinner";
import { NewChatLandingScreen } from "./NewChatDialog";

interface AddAgentDialogProps {
  parentSessionId: string;
  rootSessionId?: string;
  relatedSession?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AddAgentDialog(props: AddAgentDialogProps) {
  return props.relatedSession ? (
    <RelatedSessionDialog {...props} />
  ) : (
    <LegacyAddAgentDialog {...props} />
  );
}

function RelatedSessionDialog({
  parentSessionId,
  rootSessionId = parentSessionId,
  open,
  onOpenChange,
}: AddAgentDialogProps) {
  const { session, error } = useSession(open ? parentSessionId : null);
  const relatedSession = useMemo(
    () =>
      session
        ? { source: session, rootSessionId, onCreated: () => onOpenChange(false) }
        : undefined,
    [session, rootSessionId, onOpenChange],
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="add-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 overflow-y-auto sm:max-w-[800px]"
      >
        <DialogHeader>
          <DialogTitle>New session</DialogTitle>
        </DialogHeader>
        {open && relatedSession ? (
          <NewChatLandingScreen key={parentSessionId} relatedSession={relatedSession} />
        ) : error ? (
          <p className="text-sm text-destructive">{error.message}</p>
        ) : (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

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
function LegacyAddAgentDialog({
  parentSessionId,
  open,
  onOpenChange,
}: {
  parentSessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: agents } = useAvailableAgents();

  const agentList = agents ?? [];
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedAgent = agentList.find((a) => a.id === selectedAgentId) ?? null;

  function selectAgent(agentId: string): void {
    setSelectedAgentId(agentId);
    setError(null);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setSelectedAgentId(null);
      setName("");
      setError(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  async function handleAdd(): Promise<void> {
    if (selectedAgent === null) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a name for the agent.");
      return;
    }
    const title = `${UI_ADDED_TITLE_PREFIX}:${selectedAgent.name}:${trimmed}`;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(selectedAgent.id, [], {
        parentSessionId,
        subAgentName: null,
        title,
      });
      // Refresh the rail so the new child appears immediately, then jump
      // into it for the first message.
      await queryClient.invalidateQueries({
        queryKey: childSessionsQueryKey(parentSessionId),
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
          <DialogTitle>Add agent</DialogTitle>
        </DialogHeader>

        {/* px-1/-mx-1 give the fields' 3px focus ring room to paint:
            overflow-y-auto also clips horizontally at the padding box. */}
        <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1">
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted-foreground">Pick an agent</span>
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
                placeholder="Name this agent"
                className="rounded-md border border-input bg-background px-3 py-2 font-mono text-sm outline-none transition-colors focus-visible:border-ring"
              />
            </div>
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
            disabled={selectedAgent === null || !name.trim()}
          >
            Add
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
