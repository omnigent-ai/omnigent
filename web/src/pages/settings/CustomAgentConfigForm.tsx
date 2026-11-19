import { useEffect, useState } from "react";
import { toast } from "sonner";
import { TrashIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { BackButton } from "./HarnessCatalog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCustomAgentConfig, usePatchCustomAgent } from "@/hooks/useCustomAgents";
import { useBrainHarnessLabels } from "@/lib/agentLabels";
import {
  agentConfigDraft,
  agentConfigPatch,
  type AgentConfigDraft,
  type MCPDraft,
} from "@/lib/agentConfig";
import type { AgentConfigRevision } from "@/lib/agentsApi";
import { randomUUID } from "@/lib/randomUUID";
import { ApiError } from "@/lib/sessionsApi";
import { CUSTOM_AGENT_DETAIL_ENTRY } from "./search/catalog/customAgents";
import { settingsControlProps } from "./search/settingsControl";
import {
  SettingsDataRegion,
  SettingsOwner,
  settingsOverlayProps,
} from "./search/settingsOwnership";

export function CustomAgentConfigForm({
  id,
  name,
  onDelete,
}: {
  id: string;
  name: string;
  onDelete: () => void;
}) {
  const navigate = useNavigate();
  const query = useCustomAgentConfig(id);
  const save = usePatchCustomAgent(id);
  const labels = useBrainHarnessLabels();
  const [revision, setRevision] = useState<AgentConfigRevision | null>(null);
  const [draft, setDraft] = useState<AgentConfigDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [confirm, setConfirm] = useState<"reload" | "back" | null>(null);
  const [reloading, setReloading] = useState(false);
  const busy = save.isPending || reloading;

  useEffect(() => {
    if (query.data && !revision) {
      setRevision(query.data);
      setDraft(agentConfigDraft(query.data.config));
    }
  }, [query.data, revision]);

  let patch = {};
  let validation: string | null = null;
  if (revision && draft) {
    try {
      patch = agentConfigPatch(revision.config, draft);
    } catch (err) {
      validation = (err as Error).message;
    }
  }
  const dirty = Boolean(validation || Object.keys(patch).length);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function reload() {
    setConfirm(null);
    setReloading(true);
    const result = await query.refetch();
    if (result.isError) setError(result.error.message);
    else if (result.data) {
      setRevision(result.data);
      setDraft(agentConfigDraft(result.data.config));
      setConflict(false);
      setError(null);
      save.reset();
    }
    setReloading(false);
  }
  function request(action: "reload" | "back") {
    if (busy) return;
    if (dirty) setConfirm(action);
    else if (action === "back") navigate("/settings/custom-agents");
    else void reload();
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!revision || !dirty || validation || conflict || busy) return;
    setError(null);
    try {
      const result = await save.mutateAsync({ etag: revision.etag, patch });
      toast.success("Agent saved");
      setRevision(result);
      setDraft(agentConfigDraft(result.config));
      save.reset();
    } catch (err) {
      if (err instanceof ApiError && err.status === 412) {
        setConflict(true);
        setError(
          "This agent changed. Reload the latest configuration before saving. Your draft is still here.",
        );
      } else if (err instanceof ApiError && err.status === 428) {
        setError(
          "This server needs an update before it can save these changes. Your draft is still here. Try saving again after the update.",
        );
      } else setError(err instanceof Error ? err.message : "Could not save the agent.");
      // Mutation variables can contain replacement credentials; only the local draft retains them.
      save.reset();
    }
  }
  function updateServer(index: number, change: Partial<MCPDraft>) {
    setDraft(
      (current) =>
        current && {
          ...current,
          servers: current.servers.map((server, i) =>
            i === index ? { ...server, ...change } : server,
          ),
        },
    );
  }
  const loadError =
    query.error instanceof ApiError && query.error.status === 404
      ? "This agent was removed or is no longer accessible."
      : query.error?.message;

  return (
    <>
      <SettingsOwner entry={CUSTOM_AGENT_DETAIL_ENTRY} part="back">
        <BackButton
          label="Custom agents"
          onClick={() => request("back")}
          componentId="settings.custom_agents.back"
        />
      </SettingsOwner>
      <div className="mb-6 flex items-start justify-between gap-4">
        <h1 className="min-w-0 break-words text-2xl font-medium">{name}</h1>
        <Button
          {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "delete")}
          variant="ghost"
          size="icon"
          disabled={busy}
          onClick={onDelete}
        >
          <TrashIcon className="size-4" />
        </Button>
      </div>
      {!draft ? (
        loadError ? (
          <p role="alert">
            {loadError}{" "}
            <Button
              {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "retryConfiguration")}
              variant="link"
              onClick={() => void query.refetch()}
            >
              Retry
            </Button>
          </p>
        ) : (
          <p role="status">Loading configuration…</p>
        )
      ) : (
        <form
          onSubmit={(event) => void submit(event)}
          aria-label="Agent configuration"
          className="flex flex-col gap-6"
        >
          <fieldset disabled={busy} className="flex min-w-0 flex-col gap-5">
            <label className="space-y-1 text-ui">
              Name
              <Input
                {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "name")}
                value={name}
                readOnly
                aria-describedby="agent-name-note"
              />
              <p id="agent-name-note" className="text-xs text-muted-foreground">
                The agent name cannot be changed.
              </p>
            </label>
            <label className="space-y-1 text-ui">
              Description
              <Textarea
                {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "description")}
                maxLength={4096}
                value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              />
            </label>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1 text-ui">
                Harness
                <select
                  {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "harness")}
                  className="h-9 w-full rounded-md border border-input bg-background px-3"
                  value={draft.harness}
                  onChange={(e) => setDraft({ ...draft, harness: e.target.value })}
                >
                  {!Object.hasOwn(labels, draft.harness) && (
                    <option value={draft.harness}>{draft.harness || "Unspecified"}</option>
                  )}
                  {Object.entries(labels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1 text-ui">
                Model
                <Input
                  {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "model")}
                  value={draft.model}
                  onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                />
              </label>
            </div>
            <label className="space-y-1 text-ui">
              Instructions
              <Textarea
                {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "instructions")}
                className="min-h-32"
                value={draft.instructions}
                onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
              />
            </label>
            <div className="flex items-center justify-between">
              <h3 className="font-medium">MCP servers</h3>
              <Button
                {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "addServer")}
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  setDraft({
                    ...draft,
                    servers: [
                      ...draft.servers,
                      {
                        key: randomUUID(),
                        name: "",
                        existing: false,
                        editable: true,
                        removed: false,
                        transport: "http",
                        description: "",
                        url: "",
                        command: "",
                        args: [],
                        credentials: [],
                      },
                    ],
                  })
                }
              >
                Add server
              </Button>
            </div>
            <SettingsDataRegion
              entry={CUSTOM_AGENT_DETAIL_ENTRY}
              region="servers"
              className="contents"
            >
              {draft.servers.map((server, index) => (
                <MCPFields
                  key={server.key}
                  server={server}
                  position={index + 1}
                  onChange={(change) => updateServer(index, change)}
                  onRemove={() =>
                    server.existing
                      ? updateServer(index, { removed: !server.removed })
                      : setDraft({
                          ...draft,
                          servers: draft.servers.filter((_, i) => i !== index),
                        })
                  }
                />
              ))}
            </SettingsDataRegion>
            {!draft.servers.length && (
              <p className="text-ui text-muted-foreground">No MCP servers listed.</p>
            )}
          </fieldset>
          {(error || validation) && (
            <p role="alert" className="text-ui text-destructive">
              {error || validation}
            </p>
          )}
          <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-end gap-2 border-t border-border bg-background py-4">
            <p className="mr-auto w-full text-xs text-muted-foreground @lg:w-auto">
              Changes apply to sessions on their next turn.
            </p>
            {conflict && (
              <Button
                {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "reloadLatest")}
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => request("reload")}
              >
                Reload latest
              </Button>
            )}
            <Button
              {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "cancel")}
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => request("back")}
            >
              Cancel
            </Button>
            <Button
              {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "save")}
              type="submit"
              loading={save.isPending}
              disabled={busy || !dirty || Boolean(validation) || conflict || !revision}
            >
              Save changes
            </Button>
          </div>
        </form>
      )}
      <Dialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
      >
        <DialogContent {...settingsOverlayProps(CUSTOM_AGENT_DETAIL_ENTRY, "cancel")}>
          <DialogHeader>
            <DialogTitle>Discard unsaved changes?</DialogTitle>
            <DialogDescription>
              {confirm === "reload"
                ? "Reloading replaces your draft with the latest saved configuration."
                : "Your changes have not been saved."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              data-settings-dialog-action="Keep editing"
              variant="ghost"
              onClick={() => setConfirm(null)}
            >
              Keep editing
            </Button>
            <Button
              {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "discard")}
              variant="destructive"
              onClick={() => {
                if (confirm === "reload") void reload();
                else navigate("/settings/custom-agents");
              }}
            >
              Discard changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/* oxlint-disable react/no-array-index-key -- Argument and credential rows are fully controlled, with no child-local state. */
function MCPFields({
  server,
  position,
  onChange,
  onRemove,
}: {
  server: MCPDraft;
  /** Its place in the list, which names a new server's fields until it has a name. */
  position: number;
  onChange: (change: Partial<MCPDraft>) => void;
  onRemove: () => void;
}) {
  // Every server shows the same fields, so each is named after its own server.
  const serverName = server.name || `server ${position}`;
  return (
    <section
      data-settings-region-row
      aria-label={`MCP server ${server.name || "new"}`}
      className="space-y-3 rounded-xl border border-border p-3"
    >
      <div className="flex items-center justify-between gap-2">
        {server.existing ? (
          <h4 className="break-all font-medium">
            {server.name} <span className="text-muted-foreground">({server.transport})</span>
          </h4>
        ) : (
          <Input
            data-settings-row-field="name"
            aria-label={`Name for server ${position}`}
            placeholder="Server name"
            value={server.name}
            onChange={(e) => onChange({ name: e.target.value })}
          />
        )}
        {server.editable &&
          (server.removed ? (
            <Button
              data-settings-row-action="undoRemove"
              type="button"
              variant="ghost"
              size="sm"
              onClick={onRemove}
            >
              Undo removal
            </Button>
          ) : (
            <Button
              data-settings-row-action="remove"
              type="button"
              variant="ghost"
              size="sm"
              onClick={onRemove}
            >
              Remove server
            </Button>
          ))}
      </div>
      {!server.editable ? (
        <p className="text-ui text-muted-foreground">Managed server · read-only</p>
      ) : server.removed ? (
        <p className="text-ui text-muted-foreground">Will be removed when you save.</p>
      ) : (
        <>
          {!server.existing && (
            <label className="block text-ui">
              Transport
              <select
                data-settings-row-field="transport"
                aria-label={`Transport for ${serverName}`}
                className="ml-2 rounded-md border border-input bg-background p-1"
                value={server.transport}
                onChange={(e) => onChange({ transport: e.target.value, credentials: [] })}
              >
                <option value="http">HTTP</option>
                <option value="stdio">stdio</option>
              </select>
            </label>
          )}
          <Input
            data-settings-row-field="description"
            aria-label={`Description for ${serverName}`}
            placeholder="Description (optional)"
            maxLength={512}
            value={server.description}
            onChange={(e) => onChange({ description: e.target.value })}
          />
          {server.transport === "http" ? (
            <label className="block space-y-1 text-ui">
              URL
              <Input
                data-settings-row-field="url"
                aria-label={`URL for ${serverName}`}
                value={server.url}
                onChange={(e) => onChange({ url: e.target.value })}
              />
            </label>
          ) : (
            <>
              <label className="block space-y-1 text-ui">
                Command
                <Input
                  data-settings-row-field="command"
                  aria-label={`Command for ${serverName}`}
                  value={server.command}
                  onChange={(e) => onChange({ command: e.target.value })}
                />
              </label>
              <div className="space-y-2">
                <p className="text-ui">Arguments (one per row)</p>
                <SettingsDataRegion
                  entry={CUSTOM_AGENT_DETAIL_ENTRY}
                  region="arguments"
                  className="space-y-2"
                >
                  {server.args.map((arg, index) => (
                    <div key={index} data-settings-region-row className="flex gap-2">
                      <Input
                        data-settings-row-field="value"
                        aria-label={`Argument ${index + 1}`}
                        value={arg}
                        onChange={(e) =>
                          onChange({
                            args: server.args.map((value, i) =>
                              i === index ? e.target.value : value,
                            ),
                          })
                        }
                      />
                      <Button
                        data-settings-row-action="remove"
                        type="button"
                        variant="ghost"
                        aria-label={`Remove argument ${index + 1}`}
                        onClick={() =>
                          onChange({ args: server.args.filter((_, i) => i !== index) })
                        }
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                </SettingsDataRegion>
                <Button
                  data-settings-row-action="addArgument"
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={server.args.length >= 64}
                  onClick={() => onChange({ args: [...server.args, ""] })}
                >
                  Add argument
                </Button>
              </div>
            </>
          )}
          <div className="space-y-2">
            <p className="text-ui">
              {server.transport === "http" ? "Headers" : "Environment variables"}
            </p>
            <p className="text-xs text-muted-foreground">
              Configured values stay unchanged unless you replace or remove them.
            </p>
            <SettingsDataRegion
              entry={CUSTOM_AGENT_DETAIL_ENTRY}
              region="credentials"
              className="space-y-2"
            >
              {server.credentials.map((credential, index) => {
                const update = (change: Partial<typeof credential>) =>
                  onChange({
                    credentials: server.credentials.map((value, i) =>
                      i === index ? { ...value, ...change } : value,
                    ),
                  });
                return (
                  <div
                    key={index}
                    data-settings-region-row
                    className="space-y-2 rounded-lg border border-border p-2"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      {credential.existing ? (
                        <span className="min-w-0 flex-1 break-all text-ui">{credential.name}</span>
                      ) : (
                        <Input
                          data-settings-row-field="name"
                          aria-label={`Name for credential ${index + 1}`}
                          value={credential.name}
                          onChange={(e) => update({ name: e.target.value })}
                        />
                      )}
                      {credential.existing ? (
                        <select
                          data-settings-row-field="action"
                          aria-label={`Action for ${credential.name}`}
                          className="rounded border border-input bg-background p-1 text-ui"
                          value={credential.action}
                          onChange={(e) =>
                            update({
                              action: e.target.value as typeof credential.action,
                              value: "",
                            })
                          }
                        >
                          <option value="keep">Configured · keep</option>
                          <option value="replace">Replace</option>
                          <option value="remove">Remove</option>
                        </select>
                      ) : (
                        <Button
                          data-settings-row-action="remove"
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            onChange({
                              credentials: server.credentials.filter((_, i) => i !== index),
                            })
                          }
                        >
                          Remove credential
                        </Button>
                      )}
                    </div>
                    {credential.action === "replace" && (
                      <Input
                        data-settings-row-field="value"
                        type="password"
                        autoComplete="new-password"
                        aria-label={`Value for ${credential.name || "new credential"}`}
                        placeholder="New value (may be empty)"
                        value={credential.value}
                        onChange={(e) => update({ value: e.target.value })}
                      />
                    )}
                  </div>
                );
              })}
            </SettingsDataRegion>
            {server.transport === "http" ? (
              <Button
                data-settings-row-action="addHeader"
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  onChange({
                    credentials: [
                      ...server.credentials,
                      { name: "", value: "", action: "replace", existing: false },
                    ],
                  })
                }
              >
                Add header
              </Button>
            ) : (
              <Button
                data-settings-row-action="addVariable"
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  onChange({
                    credentials: [
                      ...server.credentials,
                      { name: "", value: "", action: "replace", existing: false },
                    ],
                  })
                }
              >
                Add variable
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}

/* oxlint-enable react/no-array-index-key */
