import { useEffect, useMemo, useState } from "react";
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
  const { session: parentSession } = useSession(open ? parentSessionId : null);

  const agentList = agents ?? [];
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedDirectoryIds, setSelectedDirectoryIds] = useState<Set<string> | null>(null);

  const selectedAgent = agentList.find((a) => a.id === selectedAgentId) ?? null;
  const parentDirectories = useMemo(
    () => parentSession?.directories ?? [],
    [parentSession?.directories],
  );

  useEffect(() => {
    if (!open || parentDirectories.length === 0 || selectedDirectoryIds !== null) return;
    setSelectedDirectoryIds(new Set(parentDirectories.map((directory) => directory.id)));
  }, [open, parentDirectories, selectedDirectoryIds]);

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
      setSelectedDirectoryIds(null);
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
    const selectedIds = parentDirectories
      .map((directory) => directory.id)
      .filter((id) => selectedDirectoryIds?.has(id) ?? true);
    const directoryIds = selectedIds.length === parentDirectories.length ? undefined : selectedIds;
    setSubmitting(true);
    setError(null);
    try {
      const session = await createSession(selectedAgent.id, [], {
        parentSessionId,
        subAgentName: null,
        title,
        ...(directoryIds === undefined ? {} : { directoryIds }),
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

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
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

          {parentDirectories.length > 0 && (
            <fieldset className="flex flex-col gap-2">
              <legend className="text-xs font-medium text-muted-foreground">Project folders</legend>
              <p className="text-[11px] text-muted-foreground">
                Choose which parent folders this agent can access. Select none for a scratch-only
                agent.
              </p>
              {parentDirectories.map((directory) => {
                const checked = selectedDirectoryIds?.has(directory.id) ?? true;
                return (
                  <label
                    key={directory.id}
                    className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => {
                        setSelectedDirectoryIds((current) => {
                          const next = new Set(current ?? parentDirectories.map((item) => item.id));
                          if (event.target.checked) next.add(directory.id);
                          else next.delete(directory.id);
                          return next;
                        });
                      }}
                      data-testid={`add-agent-directory-${directory.id}`}
                      className="mt-0.5 size-3.5"
                    />
                    <span className="min-w-0">
                      <span className="block truncate text-xs">{directory.name}</span>
                      <span className="block truncate font-mono text-[10px] text-muted-foreground">
                        {directory.path}
                      </span>
                    </span>
                  </label>
                );
              })}
            </fieldset>
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
