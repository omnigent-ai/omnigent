// Adds an agent to a card's team: a new session in the same project and folder,
// working on its own branch unless it is the lead, which edits nothing itself.

import { useEffect, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import type { Conversation } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import {
  isTeamLeadRole,
  MAX_TEAM_ROLE_LENGTH,
  teamBranchName,
  TEAM_ROLE_LABEL_KEY,
  TEAM_ROLE_PRESETS,
} from "./canvasTeam";

async function failureMessage(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  const message = (body as { error?: { message?: unknown } } | null)?.error?.message;
  return typeof message === "string" ? message : `Could not add the agent (${response.status})`;
}

export function AddAgentDialog({
  session,
  onAdded,
  onClose,
}: {
  session: Conversation | null;
  /** Reloads the board so the new card shows right away. */
  onAdded: () => Promise<void>;
  onClose: () => void;
}) {
  const open = session !== null;
  const queryClient = useQueryClient();
  const agentsQuery = useAvailableAgents({ enabled: open });
  const agents = agentsQuery.data ?? [];
  const [role, setRole] = useState<string>(TEAM_ROLE_PRESETS[0]);
  const [agentId, setAgentId] = useState("");
  const [ownWorktree, setOwnWorktree] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setRole(TEAM_ROLE_PRESETS[0]);
    setAgentId("");
    setOwnWorktree(false);
    setError(null);
  }, [open]);

  const chooseRole = (next: string) => {
    setRole(next);
    setOwnWorktree(!isTeamLeadRole(next));
  };

  const placed = Boolean(session?.host_id && session?.workspace);
  const trimmedRole = role.trim();
  const canSubmit = placed && trimmedRole.length > 0 && agentId !== "" && !submitting;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !canSubmit) return;
    setSubmitting(true);
    setError(null);
    const legacyProject = session.project_id ? undefined : session.labels?.[PROJECT_LABEL_KEY];
    try {
      const response = await authenticatedFetch("/v1/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: agentId,
          title: trimmedRole,
          host_id: session.host_id,
          workspace: session.workspace,
          ...(session.project_id ? { project_id: session.project_id } : {}),
          labels: {
            [TEAM_ROLE_LABEL_KEY]: trimmedRole,
            ...(legacyProject ? { [PROJECT_LABEL_KEY]: legacyProject } : {}),
          },
          ...(ownWorktree
            ? {
                git: {
                  branch_name: teamBranchName(trimmedRole, crypto.randomUUID().slice(0, 4)),
                },
              }
            : {}),
        }),
      });
      if (!response.ok) {
        setError(await failureMessage(response));
        return;
      }
      await Promise.all([
        onAdded(),
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
      ]);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not add the agent");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Add agent</DialogTitle>
            <DialogDescription>
              {session
                ? `Joins the team with ${conversationDisplayLabel(session)}. It can work with sessions in this project, and no others.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="team-role" className="text-ui font-medium">
              Role
            </label>
            <div className="flex flex-wrap gap-1.5">
              {TEAM_ROLE_PRESETS.map((preset) => (
                <Button
                  key={preset}
                  type="button"
                  size="sm"
                  variant={role === preset ? "default" : "outline"}
                  aria-pressed={role === preset}
                  onClick={() => chooseRole(preset)}
                >
                  {preset}
                </Button>
              ))}
            </div>
            <Input
              id="team-role"
              value={role}
              maxLength={MAX_TEAM_ROLE_LENGTH}
              onChange={(event) => chooseRole(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <span id="team-agent-label" className="text-ui font-medium">
              Agent
            </span>
            <Select value={agentId} onValueChange={setAgentId}>
              <SelectTrigger aria-labelledby="team-agent-label" data-testid="team-agent">
                <SelectValue placeholder="Choose an agent" />
              </SelectTrigger>
              <SelectContent>
                {agents.map((agent) => (
                  <SelectItem key={agent.id} value={agent.id}>
                    {agent.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-center gap-2 text-ui">
            <Checkbox
              checked={ownWorktree}
              onCheckedChange={(checked) => setOwnWorktree(checked === true)}
            />
            Work in its own git worktree
          </label>
          {!placed && (
            <p role="alert" className="text-ui text-destructive">
              This session has no connected host and folder to share.
            </p>
          )}
          {error && (
            <p role="alert" className="text-ui text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {submitting ? "Adding…" : "Add agent"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
