import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, PlusIcon, TrashIcon } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { AgentBundleInput } from "@/lib/agentBundle";
import {
  MY_AGENTS_QUERY_KEY,
  type ManagedAgent,
  createAgent,
  deleteAgent,
  listMyAgents,
} from "@/lib/agentsApi";
import { CreateAgentDialog } from "./CreateAgentDialog";

/**
 * Manage standalone, owner-scoped agents — list / create / delete.
 *
 * Opened from the sidebar "Agents" button (below "New session"). Agents
 * created here are persisted via the agents CRUD API and reused across
 * sessions, surviving session deletion. The list is a shared TanStack query
 * ({@link MY_AGENTS_QUERY_KEY}) so creates/deletes here — and from the
 * new-chat picker — keep both surfaces in sync; mutations also invalidate the
 * picker's `["available-agents"]` query so a new agent shows up there too.
 */
export function ManageAgentsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const agentsQuery = useQuery({
    queryKey: MY_AGENTS_QUERY_KEY,
    queryFn: listMyAgents,
    enabled: open,
  });
  const [actionError, setActionError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const agents = agentsQuery.isLoading ? null : (agentsQuery.data ?? []);
  const error =
    actionError ?? (agentsQuery.error instanceof Error ? agentsQuery.error.message : null);

  async function invalidateAgents() {
    // Refresh both this list and the new-chat picker catalog.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: MY_AGENTS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: ["available-agents"] }),
    ]);
  }

  async function handleCreate(input: AgentBundleInput) {
    setActionError(null);
    try {
      await createAgent(input);
      await invalidateAgents();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDelete(agent: ManagedAgent) {
    setBusyId(agent.id);
    setActionError(null);
    try {
      await deleteAgent(agent.id);
      await invalidateAgents();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
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
