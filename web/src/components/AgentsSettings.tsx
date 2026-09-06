import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";

import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { buildAgentBundle } from "@/lib/agentBundle";
import {
  CUSTOM_AGENTS_QUERY_KEY,
  createCustomAgent,
  deleteCustomAgent,
  getCustomAgent,
  importCustomAgent,
  updateCustomAgent,
  useCustomAgents,
  type CustomAgent,
} from "@/lib/customAgentsApi";
import { CreateAgentDialog } from "@/shell/CreateAgentDialog";

import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "The Agent could not be saved.";
}

export function AgentsSettings() {
  const queryClient = useQueryClient();
  const catalog = useCustomAgents();
  const available = useAvailableAgents();
  const customAgents = useMemo(() => catalog.data ?? [], [catalog.data]);
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<CustomAgent | null>(null);
  const [deleting, setDeleting] = useState<CustomAgent | null>(null);
  const [importingId, setImportingId] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: CUSTOM_AGENTS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: ["available-agents"] }),
    ]);
  }

  const sessionAgents = useMemo(() => {
    const liveTemplateIds = new Set(customAgents.map((agent) => agent.id));
    return (available.data ?? []).filter(
      (agent) =>
        agent.sessionId !== undefined &&
        (agent.templateId === undefined || !liveTemplateIds.has(agent.templateId)),
    );
  }, [available.data, customAgents]);

  async function importAgent(agent: AvailableAgent) {
    if (!agent.sessionId || importingId !== null) return;
    setImportingId(agent.id);
    setError(null);
    try {
      await importCustomAgent(agent.sessionId);
      await refresh();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setImportingId(null);
    }
  }

  return (
    <section aria-label="Agents" className="mx-auto w-full max-w-3xl space-y-7">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Agents</h1>
          <p className="text-sm text-muted-foreground">
            Save private Agent configurations and reuse them in new sessions.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={catalog.isLoading || !!catalog.error}
          onClick={() => {
            setError(null);
            setCreateOpen(true);
          }}
        >
          <PlusIcon className="size-3.5" />
          New Agent
        </Button>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium">Your Agents</h2>
        {catalog.isLoading && (
          <p role="status" className="py-4 text-sm text-muted-foreground">
            Loading Agents…
          </p>
        )}
        {catalog.error && (
          <div role="alert" className="space-y-2 py-4 text-sm text-destructive">
            <p>{errorText(catalog.error)}</p>
            <Button variant="outline" size="sm" onClick={() => void catalog.refetch()}>
              Retry
            </Button>
          </div>
        )}
        {!catalog.isLoading && !catalog.error && customAgents.length === 0 && (
          <p className="py-4 text-sm text-muted-foreground">No saved Agents yet.</p>
        )}
        {customAgents.map((agent) => (
          <div key={agent.id} className="flex min-h-14 items-center gap-2 border-b py-2">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{agent.name}</div>
              <div className="truncate text-xs text-muted-foreground">
                {[agent.description, agent.harness, agent.model].filter(Boolean).join(" · ")}
              </div>
            </div>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Edit ${agent.name}`}
              onClick={() => {
                setError(null);
                setEditing(agent);
              }}
            >
              Edit
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive"
              aria-label={`Delete ${agent.name}`}
              onClick={() => {
                setError(null);
                setDeleting(agent);
              }}
            >
              Delete
            </Button>
          </div>
        ))}
      </div>

      {sessionAgents.length > 0 && !catalog.error && (
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">Import from existing sessions</summary>
          <p className="mt-1 text-xs text-muted-foreground">
            Save a session-scoped Agent so it can be selected in future sessions.
          </p>
          {sessionAgents.map((agent) => (
            <div key={agent.id} className="mt-2 flex min-h-12 items-center gap-2 border-b py-2">
              <span className="min-w-0 flex-1 truncate">{agent.display_name}</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={importingId !== null}
                onClick={() => void importAgent(agent)}
              >
                {importingId === agent.id ? "Importing…" : "Import"}
              </Button>
            </div>
          ))}
        </details>
      )}

      {error && !deleting && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <CreateAgentDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreate={async (input) => {
          await createCustomAgent(await buildAgentBundle(input));
          await refresh();
        }}
      />

      {editing && (
        <AgentSettingsEditor
          key={editing.id}
          agent={editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open && !deleteBusy) setDeleting(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name}?</DialogTitle>
            <DialogDescription>
              Remove this Agent from your library. Existing sessions keep their saved copy.
            </DialogDescription>
          </DialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" disabled={deleteBusy} onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteBusy}
              onClick={async () => {
                if (!deleting || deleteBusy) return;
                setDeleteBusy(true);
                setError(null);
                try {
                  await deleteCustomAgent(deleting.id);
                  await refresh();
                  setDeleting(null);
                } catch (cause) {
                  setError(errorText(cause));
                } finally {
                  setDeleteBusy(false);
                }
              }}
            >
              {deleteBusy ? "Deleting…" : "Delete Agent"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function AgentSettingsEditor({
  agent,
  onClose,
  onSaved,
}: {
  agent: CustomAgent;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const detail = useQuery({
    queryKey: ["custom-agent", agent.id],
    queryFn: () => getCustomAgent(agent.id),
    staleTime: 0,
  });
  const [name, setName] = useState(agent.name);
  const [description, setDescription] = useState(agent.description ?? "");
  const [instructions, setInstructions] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!detail.data) return;
    setName(detail.data.name);
    setDescription(detail.data.description ?? "");
    setInstructions(detail.data.instructions ?? "");
  }, [detail.data]);

  const unavailable = !detail.data || detail.isFetching;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit Agent</DialogTitle>
          <DialogDescription>
            Changes apply when this saved Agent is selected for a new session.
          </DialogDescription>
        </DialogHeader>
        <div className="-mx-3 -my-2 min-h-0 space-y-4 overflow-x-hidden overflow-y-auto px-3 py-2">
          {detail.isLoading && <p role="status">Loading Agent…</p>}
          {detail.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(detail.error)}
            </p>
          )}
          <label className="block space-y-1.5 text-sm">
            <span>Name</span>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={unavailable || busy}
            />
          </label>
          <label className="block space-y-1.5 text-sm">
            <span>Description</span>
            <Input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              disabled={unavailable || busy}
            />
          </label>
          <label className="block space-y-1.5 text-sm">
            <span>Instructions</span>
            <Textarea
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              disabled={unavailable || busy}
              className="min-h-32"
            />
          </label>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={busy || unavailable || !name.trim()}
            onClick={async () => {
              if (busy || !detail.data) return;
              setBusy(true);
              setError(null);
              try {
                await updateCustomAgent(agent.id, {
                  name: name.trim(),
                  description: description.trim() || null,
                  instructions: instructions || null,
                  version: detail.data.version,
                });
                await onSaved();
                onClose();
              } catch (cause) {
                setError(errorText(cause));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
