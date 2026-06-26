import { useCallback, useEffect, useState } from "react";
import { Bot, PlusIcon, TrashIcon } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { AgentBundleInput } from "@/lib/agentBundle";
import { type ManagedAgent, createAgent, deleteAgent, listMyAgents } from "@/lib/agentsApi";
import { CreateAgentDialog } from "./CreateAgentDialog";

/**
 * Manage standalone, owner-scoped agents — list / create / delete.
 *
 * Opened from the sidebar "Agents" button (below "New session"). Unlike the
 * new-chat custom-agent flow (which stages an agent into a single session),
 * agents created here are persisted via the agents CRUD API and reused across
 * sessions, surviving session deletion. Create reuses {@link CreateAgentDialog}
 * but writes straight to the backend instead of staging into a session.
 */
export function ManageAgentsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [agents, setAgents] = useState<ManagedAgent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setAgents(await listMyAgents());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  async function handleCreate(input: AgentBundleInput) {
    setError(null);
    try {
      await createAgent(input);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDelete(agent: ManagedAgent) {
    setBusyId(agent.id);
    setError(null);
    try {
      await deleteAgent(agent.id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          data-testid="manage-agents-dialog"
          className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
        >
          <DialogHeader>
            <DialogTitle>Agents</DialogTitle>
          </DialogHeader>

          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">
              Reusable agents you own — they persist across sessions and aren't tied to any chat.
            </p>
            <Button
              size="sm"
              className="shrink-0 gap-1.5"
              data-testid="manage-agents-new"
              onClick={() => setCreateOpen(true)}
            >
              <PlusIcon className="size-4" />
              New agent
            </Button>
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
            {agents === null ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : agents.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No agents yet. Create one to reuse it across sessions.
              </p>
            ) : (
              agents.map((agent) => (
                <div
                  key={agent.id}
                  data-testid="manage-agents-row"
                  className="flex items-center gap-3 rounded-md border border-border p-3"
                >
                  <Bot className="size-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{agent.name}</div>
                    {agent.description && (
                      <div className="truncate text-xs text-muted-foreground">
                        {agent.description}
                      </div>
                    )}
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      {agent.harness ?? "agent"}
                      {agent.mcp_servers.length > 0 &&
                        ` · ${agent.mcp_servers.length} MCP server${
                          agent.mcp_servers.length === 1 ? "" : "s"
                        }`}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="shrink-0"
                    aria-label={`Delete ${agent.name}`}
                    disabled={busyId === agent.id}
                    onClick={() => void handleDelete(agent)}
                  >
                    <TrashIcon className="size-4 text-destructive" />
                  </Button>
                </div>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>

      <CreateAgentDialog open={createOpen} onOpenChange={setCreateOpen} onCreate={handleCreate} />
    </>
  );
}
