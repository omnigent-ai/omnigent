import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PlusIcon, TrashIcon } from "lucide-react";
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
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { showToast } from "@/components/ui/toast";
import {
  MY_MCP_SERVERS_QUERY_KEY,
  getMcpServerConfig,
  listMyMcpServers,
  type McpServerFullConfig,
} from "@/lib/mcpApi";
import type { AgentBundleInput, MCPServerInput } from "@/lib/agentBundle";

/**
 * Harness options for the picker. "default" uses the server's default
 * executor (no explicit harness in the bundle).
 */
const HARNESS_OPTIONS: { value: string; label: string }[] = Object.entries(
  BRAIN_HARNESS_LABELS,
).map(([value, label]) => ({ value, label }));

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

/** Parse "KEY=VAL" lines into a Record. */
function parseKVLines(text: string): Record<string, string> | undefined {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length === 0) return undefined;
  const result: Record<string, string> = {};
  for (const line of lines) {
    const eq = line.indexOf("=");
    if (eq > 0) {
      result[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
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

/** Convert a stored MCP server's full config to the bundle input shape. */
function fullConfigToMCPInput(config: McpServerFullConfig): MCPServerInput {
  if (config.transport === "stdio") {
    return {
      name: config.name,
      transport: "stdio",
      command: config.command ?? "",
      args: config.args,
      env: Object.keys(config.env).length > 0 ? config.env : undefined,
    };
  }
  return {
    name: config.name,
    transport: "http",
    url: config.url ?? "",
    headers: Object.keys(config.headers).length > 0 ? config.headers : undefined,
  };
}

/**
 * Dialog for creating a custom agent from the new-session picker.
 *
 * Collects a name, optional description, optional system instructions,
 * a harness choice, and zero or more MCP server declarations. On submit,
 * passes the agent configuration back to the parent via `onCreate` so it
 * can build a bundle and start a session with it.
 */
export function CreateAgentDialog({
  open,
  onOpenChange,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (input: AgentBundleInput) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [harness, setHarness] = useState(HARNESS_OPTIONS[0].value);
  const [model, setModel] = useState("");
  const [mcpEntries, setMcpEntries] = useState<MCPFormEntry[]>([]);
  const [nextKey, setNextKey] = useState(0);
  const [selectedMcpIds, setSelectedMcpIds] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);

  // The caller's preconfigured MCP servers, selectable instead of typed.
  const mcpServersQuery = useQuery({
    queryKey: MY_MCP_SERVERS_QUERY_KEY,
    queryFn: listMyMcpServers,
    enabled: open,
  });

  function reset() {
    setName("");
    setDescription("");
    setInstructions("");
    setHarness(HARNESS_OPTIONS[0].value);
    setModel("");
    setMcpEntries([]);
    setNextKey(0);
    setSelectedMcpIds(new Set());
  }

  function toggleSelectedMcp(id: string) {
    setSelectedMcpIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
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

  async function handleSubmit() {
    const trimmedName = name.trim();
    if (!trimmedName) return;

    setSubmitting(true);
    try {
      // Resolve selected preconfigured servers (incl. secrets) and merge
      // with the manually-entered rows. A manual entry with the same name
      // wins, so the inline form can override a preconfigured server.
      let preconfigured: MCPServerInput[] = [];
      if (selectedMcpIds.size > 0) {
        const configs = await Promise.all(
          Array.from(selectedMcpIds).map(getMcpServerConfig),
        );
        preconfigured = configs.map(fullConfigToMCPInput);
      }
      const manual = toMCPInputs(mcpEntries) ?? [];
      const manualNames = new Set(manual.map((m) => m.name));
      const merged = [...preconfigured.filter((p) => !manualNames.has(p.name)), ...manual];

      onCreate({
        name: trimmedName,
        description: description.trim() || undefined,
        instructions: instructions.trim() || undefined,
        harness,
        model: model.trim(),
        mcpServers: merged.length > 0 ? merged : undefined,
      });
      reset();
      onOpenChange(false);
    } catch (err) {
      showToast(
        `Could not load selected MCP servers: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSubmitting(false);
    }
  }

  const canSubmit = name.trim().length > 0 && model.trim().length > 0 && !submitting;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="create-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Create custom agent</DialogTitle>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          {/* Name */}
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="create-agent-name"
              className="text-xs font-medium text-muted-foreground"
            >
              Name <span className="text-destructive">*</span>
            </label>
            <Input
              id="create-agent-name"
              data-testid="create-agent-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-agent"
              autoFocus
            />
          </div>

          {/* Description */}
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="create-agent-description"
              className="text-xs font-medium text-muted-foreground"
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
            <label className="text-xs font-medium text-muted-foreground">
              Harness <span className="text-destructive">*</span>
            </label>
            <Select value={harness} onValueChange={setHarness}>
              <SelectTrigger data-testid="create-agent-harness" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {HARNESS_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Model */}
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="create-agent-model"
              className="text-xs font-medium text-muted-foreground"
            >
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
              className="text-xs font-medium text-muted-foreground"
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
            />
          </div>

          {/* Preconfigured MCP servers — select from the user's registered
          servers instead of re-typing url/headers. */}
          {(mcpServersQuery.data?.length ?? 0) > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">
                Preconfigured MCP servers
              </span>
              <div className="flex flex-col gap-1 rounded-md border border-border p-2">
                {mcpServersQuery.data?.map((server) => (
                  <label
                    key={server.id}
                    className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-muted"
                    data-testid="create-agent-preconfigured-mcp"
                  >
                    <input
                      type="checkbox"
                      checked={selectedMcpIds.has(server.id)}
                      onChange={() => toggleSelectedMcp(server.id)}
                      className="size-3.5 accent-primary"
                    />
                    <span className="truncate">{server.name}</span>
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {server.transport}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* MCP Servers */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">MCP Tools</span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={addMCPServer}
                data-testid="create-agent-add-mcp"
                className="h-6 gap-1 px-2 text-xs text-muted-foreground"
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
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            data-testid="create-agent-submit"
            onClick={() => void handleSubmit()}
            disabled={!canSubmit}
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
          data-testid="create-agent-mcp-name"
          value={entry.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="server-name"
          className="flex-1"
        />
        <Select
          value={entry.transport}
          onValueChange={(v: "http" | "stdio") => onChange({ transport: v })}
        >
          <SelectTrigger data-testid="create-agent-mcp-transport" className="w-24">
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
          data-testid="create-agent-mcp-remove"
          className="size-7 text-muted-foreground hover:text-destructive"
        >
          <TrashIcon className="size-3.5" />
        </Button>
      </div>

      {entry.transport === "stdio" ? (
        <>
          <Input
            data-testid="create-agent-mcp-command"
            value={entry.command}
            onChange={(e) => onChange({ command: e.target.value })}
            placeholder="command (e.g. npx)"
          />
          <Input
            data-testid="create-agent-mcp-args"
            value={entry.args}
            onChange={(e) => onChange({ args: e.target.value })}
            placeholder="args (e.g. -y @modelcontextprotocol/server-github)"
          />
          <Textarea
            data-testid="create-agent-mcp-env"
            value={entry.env}
            onChange={(e) => onChange({ env: e.target.value })}
            placeholder={"Environment variables (KEY=VALUE per line)\ne.g. GITHUB_TOKEN=ghp_..."}
            className="min-h-[60px] font-mono text-xs"
          />
        </>
      ) : (
        <>
          <Input
            data-testid="create-agent-mcp-url"
            value={entry.url}
            onChange={(e) => onChange({ url: e.target.value })}
            placeholder="https://mcp.example.com/sse"
          />
          <Textarea
            data-testid="create-agent-mcp-headers"
            value={entry.headers}
            onChange={(e) => onChange({ headers: e.target.value })}
            placeholder={"HTTP headers (KEY=VALUE per line)\ne.g. Authorization=Bearer tok_..."}
            className="min-h-[60px] font-mono text-xs"
          />
        </>
      )}
    </div>
  );
}
