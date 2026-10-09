import { useRef, useState } from "react";
import { PlusIcon, TrashIcon, UploadIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { BRAIN_HARNESS_LABELS, useBrainHarnessLabels } from "@/lib/agentLabels";
import type { AgentBundleInput, MCPServerInput } from "@/lib/agentBundle";

/**
 * Harness options for the picker. "default" uses the server's default
 * executor (no explicit harness in the bundle).
 */
const DEFAULT_HARNESS = Object.keys(BRAIN_HARNESS_LABELS)[0];

/** The server's rule for agent names (`omnigent/spec/validator.py`). */
const AGENT_NAME_PATTERN = /^[a-zA-Z0-9_-]+$/;

/** A single MCP server row in the form. */
interface MCPFormEntry {
  /** Stable key for React list rendering. */
  key: number;
  name: string;
  transport: "http" | "stdio";
  url: string;
  headers: string;
  command: string;
  args: string;
  env: string;
}

function emptyMCPEntry(key: number): MCPFormEntry {
  return {
    key,
    name: "",
    transport: "stdio",
    url: "",
    headers: "",
    command: "",
    args: "",
    env: "",
  };
}

/** Parse "KEY=VAL" or "KEY: VAL" lines into a Record. */
function parseKVLines(text: string): Record<string, string> | undefined {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length === 0) return undefined;
  const result: Record<string, string> = {};
  for (const line of lines) {
    // Support both KEY=VALUE (env-var style) and KEY: VALUE (HTTP header style).
    const eq = line.indexOf("=");
    const colon = line.indexOf(":");
    let sep = -1;
    if (eq > 0 && colon > 0) sep = Math.min(eq, colon);
    else if (eq > 0) sep = eq;
    else if (colon > 0) sep = colon;
    if (sep > 0) {
      result[line.slice(0, sep).trim()] = line.slice(sep + 1).trim();
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

/** Convert form entries to the bundle input shape. */
function toMCPInputs(entries: MCPFormEntry[]): MCPServerInput[] | undefined {
  const result: MCPServerInput[] = [];
  for (const e of entries) {
    const name = e.name.trim();
    if (!name) continue;
    if (e.transport === "stdio") {
      const command = e.command.trim();
      if (!command) continue;
      result.push({
        name,
        transport: "stdio",
        command,
        args: e.args
          .split(/\s+/)
          .map((a) => a.trim())
          .filter(Boolean),
        env: parseKVLines(e.env),
      });
    } else {
      const url = e.url.trim();
      if (!url) continue;
      result.push({
        name,
        transport: "http",
        url,
        headers: parseKVLines(e.headers),
      });
    }
  }
  return result.length > 0 ? result : undefined;
}

/** Collect a draft for a new session, or import an installed agent bundle. */
export function CreateAgentDialog({
  open,
  onOpenChange,
  onCreate,
  onImport,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (input: AgentBundleInput) => void;
  /** Install a picked bundle; rejects with a user-facing message on failure. */
  onImport?: (bundle: File) => Promise<void>;
}) {
  const fields = useAgentFormFields();
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function reset() {
    fields.reset();
    setImportError(null);
  }

  function handleOpenChange(next: boolean) {
    // Stay open while an import is in flight so its result lands somewhere.
    if (!next && importing) return;
    if (!next) reset();
    onOpenChange(next);
  }

  function handleSubmit() {
    if (!AGENT_NAME_PATTERN.test(fields.name.trim())) return;
    onCreate(fields.toInput());
    reset();
    onOpenChange(false);
  }

  async function handleImport(bundle: File | undefined) {
    if (!bundle || !onImport) return;
    setImporting(true);
    setImportError(null);
    try {
      await onImport(bundle);
      reset();
      onOpenChange(false);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
      // Clear so re-picking the same file after a fix fires onChange again.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="create-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Create custom agent</DialogTitle>
        </DialogHeader>

        {/* px-1/-mx-1 give the fields' 3px focus ring room to paint:
            overflow-y-auto also clips horizontally at the padding box. */}
        <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1">
          <AgentFormFields {...fields} />
        </div>

        {importError && (
          <p
            data-testid="create-agent-import-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {importError}
          </p>
        )}

        <DialogFooter>
          {onImport && (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept=".tar.gz,.tgz,application/gzip"
                className="hidden"
                data-testid="create-agent-import-input"
                onChange={(e) => void handleImport(e.target.files?.[0])}
              />
              <Button
                variant="outline"
                className="sm:mr-auto"
                disabled={importing}
                data-testid="create-agent-import"
                onClick={() => fileInputRef.current?.click()}
              >
                <UploadIcon className="size-3.5" />
                {importing ? "Importing…" : "Import bundle"}
              </Button>
            </>
          )}
          <Button variant="ghost" disabled={importing} onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            data-testid="create-agent-submit"
            onClick={handleSubmit}
            disabled={!fields.canSubmit || importing}
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Settings saves persist the agent and keep the draft available on failure. */
export function CreateAgentForm({
  onCreate,
  onCancel,
  notice,
  submitLabel = "Create",
}: {
  onCreate: (input: AgentBundleInput) => void | Promise<void>;
  onCancel: () => void;
  notice?: string;
  submitLabel?: string;
}) {
  const fields = useAgentFormFields();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (pending || !fields.canSubmit) return;
    setPending(true);
    setError(null);
    try {
      await onCreate(fields.toInput());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the agent.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={(event) => void handleSubmit(event)} className="flex min-h-0 flex-col gap-4">
      {notice && <p className="text-ui text-muted-foreground">{notice}</p>}
      <div className="-mx-1 flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto px-1">
        <fieldset disabled={pending} className="contents">
          <AgentFormFields {...fields} />
        </fieldset>
      </div>
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button
          type="submit"
          data-testid="create-agent-submit"
          loading={pending}
          disabled={!fields.canSubmit || pending}
        >
          {submitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

function useAgentFormFields() {
  const brainHarnessLabels = useBrainHarnessLabels();
  const harnessOptions = Object.entries(brainHarnessLabels).map(([value, label]) => ({
    value,
    label,
  }));
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [harness, setHarness] = useState(DEFAULT_HARNESS);
  const [model, setModel] = useState("");
  const [mcpEntries, setMcpEntries] = useState<MCPFormEntry[]>([]);
  const [nextKey, setNextKey] = useState(0);

  function reset() {
    setName("");
    setDescription("");
    setInstructions("");
    setHarness(DEFAULT_HARNESS);
    setModel("");
    setMcpEntries([]);
    setNextKey(0);
  }

  function addMCPServer() {
    setMcpEntries((prev) => [...prev, emptyMCPEntry(nextKey)]);
    setNextKey((k) => k + 1);
  }

  function removeMCPServer(key: number) {
    setMcpEntries((prev) => prev.filter((e) => e.key !== key));
  }

  function updateMCPEntry(key: number, patch: Partial<MCPFormEntry>) {
    setMcpEntries((prev) => prev.map((e) => (e.key === key ? { ...e, ...patch } : e)));
  }

  const nameInvalid = name.trim().length > 0 && !AGENT_NAME_PATTERN.test(name.trim());
  const canSubmit = AGENT_NAME_PATTERN.test(name.trim()) && model.trim().length > 0;

  function toInput(): AgentBundleInput {
    return {
      name: name.trim(),
      description: description.trim() || undefined,
      instructions: instructions.trim() || undefined,
      harness,
      model: model.trim(),
      mcpServers: toMCPInputs(mcpEntries),
    };
  }

  return {
    name,
    setName,
    description,
    setDescription,
    instructions,
    setInstructions,
    harness,
    setHarness,
    harnessOptions,
    model,
    setModel,
    mcpEntries,
    addMCPServer,
    removeMCPServer,
    updateMCPEntry,
    nameInvalid,
    canSubmit,
    reset,
    toInput,
  };
}

function AgentFormFields({
  name,
  setName,
  description,
  setDescription,
  instructions,
  setInstructions,
  harness,
  setHarness,
  harnessOptions,
  model,
  setModel,
  mcpEntries,
  addMCPServer,
  removeMCPServer,
  updateMCPEntry,
  nameInvalid,
}: ReturnType<typeof useAgentFormFields>) {
  return (
    <>
      {/* Name */}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="create-agent-name" className="text-sm font-medium text-muted-foreground">
          Name <span className="text-destructive">*</span>
        </label>
        <Input
          id="create-agent-name"
          data-testid="create-agent-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my-agent"
          aria-invalid={nameInvalid || undefined}
          aria-describedby={nameInvalid ? "create-agent-name-error" : undefined}
          autoFocus
        />
        {nameInvalid && (
          <p
            id="create-agent-name-error"
            data-testid="create-agent-name-error"
            className="text-xs text-destructive"
          >
            Use only letters, numbers, hyphens, and underscores.
          </p>
        )}
      </div>

      {/* Description */}
      <div className="flex flex-col gap-1.5">
        <label
          htmlFor="create-agent-description"
          className="text-sm font-medium text-muted-foreground"
        >
          Description
        </label>
        <Input
          id="create-agent-description"
          data-testid="create-agent-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="A short summary of what this agent does"
        />
      </div>

      {/* Harness */}
      <div className="flex flex-col gap-1.5">
        <label className="text-sm font-medium text-muted-foreground">
          Harness <span className="text-destructive">*</span>
        </label>
        <Select
          value={harness}
          onValueChange={setHarness}
          componentId="create_agent.harness"
          valueHasNoPii
        >
          <SelectTrigger aria-label="Harness" data-testid="create-agent-harness" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {harnessOptions.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Model */}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="create-agent-model" className="text-sm font-medium text-muted-foreground">
          Model <span className="text-destructive">*</span>
        </label>
        <Input
          id="create-agent-model"
          data-testid="create-agent-model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="claude-sonnet-4-20250514"
        />
      </div>

      {/* Instructions / System Prompt */}
      <div className="flex flex-col gap-1.5">
        <label
          htmlFor="create-agent-instructions"
          className="text-sm font-medium text-muted-foreground"
        >
          System instructions
        </label>
        <Textarea
          id="create-agent-instructions"
          data-testid="create-agent-instructions"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="You are a helpful assistant that..."
          className="min-h-[120px]"
          componentId="create_agent.instructions"
        />
      </div>

      {/* MCP Servers */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-muted-foreground">MCP Tools</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={addMCPServer}
            data-testid="create-agent-add-mcp"
            className="h-6 gap-1 px-2 text-sm text-muted-foreground"
          >
            <PlusIcon className="size-3" />
            Add server
          </Button>
        </div>
        {mcpEntries.map((entry) => (
          <MCPServerRow
            key={entry.key}
            entry={entry}
            onChange={(patch) => updateMCPEntry(entry.key, patch)}
            onRemove={() => removeMCPServer(entry.key)}
          />
        ))}
      </div>
    </>
  );
}

/** A single MCP server entry in the form. */
function MCPServerRow({
  entry,
  onChange,
  onRemove,
}: {
  entry: MCPFormEntry;
  onChange: (patch: Partial<MCPFormEntry>) => void;
  onRemove: () => void;
}) {
  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-border p-3"
      data-testid="create-agent-mcp-entry"
    >
      <div className="flex items-center gap-2">
        <Input
          aria-label="Server name"
          data-testid="create-agent-mcp-name"
          value={entry.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="server-name"
          className="flex-1"
        />
        <Select
          value={entry.transport}
          onValueChange={(v: "http" | "stdio") => onChange({ transport: v })}
          componentId="create_agent.mcp_transport"
          valueHasNoPii
        >
          <SelectTrigger
            aria-label="Transport"
            data-testid="create-agent-mcp-transport"
            className="w-24"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="stdio">stdio</SelectItem>
            <SelectItem value="http">http</SelectItem>
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onRemove}
          aria-label="Remove server"
          data-testid="create-agent-mcp-remove"
          className="size-7 text-muted-foreground hover:text-destructive"
        >
          <TrashIcon className="size-3.5" />
        </Button>
      </div>

      {entry.transport === "stdio" ? (
        <>
          <Input
            aria-label="Command"
            data-testid="create-agent-mcp-command"
            value={entry.command}
            onChange={(e) => onChange({ command: e.target.value })}
            placeholder="command (e.g. npx)"
          />
          <Input
            aria-label="Arguments"
            data-testid="create-agent-mcp-args"
            value={entry.args}
            onChange={(e) => onChange({ args: e.target.value })}
            placeholder="args (e.g. -y @modelcontextprotocol/server-github)"
          />
          <Textarea
            aria-label="Environment variables"
            data-testid="create-agent-mcp-env"
            value={entry.env}
            onChange={(e) => onChange({ env: e.target.value })}
            placeholder={"Environment variables (KEY=VALUE per line)\ne.g. GITHUB_TOKEN=ghp_..."}
            className="min-h-[60px] font-mono text-sm"
          />
        </>
      ) : (
        <>
          <Input
            aria-label="Server URL"
            data-testid="create-agent-mcp-url"
            value={entry.url}
            onChange={(e) => onChange({ url: e.target.value })}
            placeholder="https://mcp.example.com/sse"
          />
          <Textarea
            aria-label="HTTP headers"
            data-testid="create-agent-mcp-headers"
            value={entry.headers}
            onChange={(e) => onChange({ headers: e.target.value })}
            placeholder={"HTTP headers (one per line)\ne.g. Authorization: Bearer tok_..."}
            className="min-h-[60px] font-mono text-sm"
          />
        </>
      )}
    </div>
  );
}
