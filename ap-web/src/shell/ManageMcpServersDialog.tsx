import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, Loader2Icon, PlugIcon, TrashIcon, XCircleIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
import { showToast } from "@/components/ui/toast";
import {
  MY_MCP_SERVERS_QUERY_KEY,
  createMcpServer,
  deleteMcpServer,
  listMyMcpServers,
  verifyMcpServer,
  verifySavedMcpServer,
  type McpServerInput,
  type McpServerObject,
  type McpVerifyResult,
} from "@/lib/mcpApi";

/** Parse "KEY=VAL" lines into a Record (undefined when empty). */
function parseKVLines(text: string): Record<string, string> | undefined {
  const result: Record<string, string> = {};
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const eq = line.indexOf("=");
    if (eq > 0) result[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

interface FormState {
  name: string;
  transport: "http" | "stdio";
  url: string;
  headers: string;
  command: string;
  args: string;
  env: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  transport: "http",
  url: "",
  headers: "",
  command: "",
  args: "",
  env: "",
};

/** Build the API write body from the add-form state (null if incomplete). */
function formToInput(form: FormState): McpServerInput | null {
  const name = form.name.trim();
  if (!name) return null;
  if (form.transport === "http") {
    const url = form.url.trim();
    if (!url) return null;
    return { name, transport: "http", url, headers: parseKVLines(form.headers) };
  }
  const command = form.command.trim();
  if (!command) return null;
  return {
    name,
    transport: "stdio",
    command,
    args: form.args.split(/\s+/).map((a) => a.trim()).filter(Boolean),
    env: parseKVLines(form.env),
  };
}

/**
 * Sidebar dialog to manage standalone, reusable MCP servers: list, add a
 * remote server, verify a connection (showing the tool list), and delete.
 * Decoupled from any session — these are registered once and selected when
 * creating agents.
 */
export function ManageMcpServersDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const serversQuery = useQuery({
    queryKey: MY_MCP_SERVERS_QUERY_KEY,
    queryFn: listMyMcpServers,
    enabled: open,
  });

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  // Verify state keyed by server id, or "new" for the add-form draft.
  const [verifyResults, setVerifyResults] = useState<Record<string, McpVerifyResult>>({});
  const [verifying, setVerifying] = useState<string | null>(null);

  function patchForm(patch: Partial<FormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  const createMutation = useMutation({
    mutationFn: createMcpServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MY_MCP_SERVERS_QUERY_KEY });
      setForm(EMPTY_FORM);
      setVerifyResults((prev) => {
        const next = { ...prev };
        delete next.new;
        return next;
      });
      showToast("MCP server added");
    },
    onError: (err: unknown) => {
      showToast(`Could not add MCP server: ${err instanceof Error ? err.message : String(err)}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteMcpServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MY_MCP_SERVERS_QUERY_KEY });
    },
    onError: (err: unknown) => {
      showToast(
        `Could not delete MCP server: ${err instanceof Error ? err.message : String(err)}`,
      );
    },
  });

  async function runVerify(key: string, fn: () => Promise<McpVerifyResult>) {
    setVerifying(key);
    try {
      const result = await fn();
      setVerifyResults((prev) => ({ ...prev, [key]: result }));
    } catch (err) {
      setVerifyResults((prev) => ({
        ...prev,
        [key]: {
          ok: false,
          tools: [],
          error: err instanceof Error ? err.message : String(err),
        },
      }));
    } finally {
      setVerifying(null);
    }
  }

  const draftInput = formToInput(form);

  function handleOpenChange(next: boolean) {
    if (!next) {
      setForm(EMPTY_FORM);
      setVerifyResults({});
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="manage-mcp-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>MCP servers</DialogTitle>
          <DialogDescription>
            Reusable connections to Model Context Protocol servers. Verify a connection to see
            its tools, then select it when creating an agent.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          {/* Existing servers */}
          <div className="flex flex-col gap-2">
            {serversQuery.isLoading ? (
              <p className="text-xs text-muted-foreground">Loading…</p>
            ) : (serversQuery.data?.length ?? 0) === 0 ? (
              <p className="text-xs text-muted-foreground">No MCP servers yet.</p>
            ) : (
              serversQuery.data?.map((server) => (
                <ServerRow
                  key={server.id}
                  server={server}
                  verifyResult={verifyResults[server.id]}
                  verifying={verifying === server.id}
                  onVerify={() =>
                    runVerify(server.id, () => verifySavedMcpServer(server.id))
                  }
                  onDelete={() => deleteMutation.mutate(server.id)}
                  deleting={deleteMutation.isPending && deleteMutation.variables === server.id}
                />
              ))
            )}
          </div>

          {/* Add a server */}
          <div className="flex flex-col gap-2 rounded-md border border-border p-3">
            <span className="text-xs font-medium text-muted-foreground">Add a server</span>
            <div className="flex items-center gap-2">
              <Input
                data-testid="mcp-add-name"
                value={form.name}
                onChange={(e) => patchForm({ name: e.target.value })}
                placeholder="server-name"
                className="flex-1"
              />
              <Select
                value={form.transport}
                onValueChange={(v: "http" | "stdio") => patchForm({ transport: v })}
              >
                <SelectTrigger data-testid="mcp-add-transport" className="w-24">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="http">http</SelectItem>
                  <SelectItem value="stdio">stdio</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {form.transport === "http" ? (
              <>
                <Input
                  data-testid="mcp-add-url"
                  value={form.url}
                  onChange={(e) => patchForm({ url: e.target.value })}
                  placeholder="https://mcp.example.com/sse"
                />
                <Textarea
                  data-testid="mcp-add-headers"
                  value={form.headers}
                  onChange={(e) => patchForm({ headers: e.target.value })}
                  placeholder={"HTTP headers (KEY=VALUE per line)\ne.g. Authorization=Bearer tok_..."}
                  className="min-h-[60px] font-mono text-xs"
                />
              </>
            ) : (
              <>
                <Input
                  data-testid="mcp-add-command"
                  value={form.command}
                  onChange={(e) => patchForm({ command: e.target.value })}
                  placeholder="command (e.g. npx)"
                />
                <Input
                  data-testid="mcp-add-args"
                  value={form.args}
                  onChange={(e) => patchForm({ args: e.target.value })}
                  placeholder="args (e.g. -y @modelcontextprotocol/server-github)"
                />
                <Textarea
                  data-testid="mcp-add-env"
                  value={form.env}
                  onChange={(e) => patchForm({ env: e.target.value })}
                  placeholder={"Environment variables (KEY=VALUE per line)"}
                  className="min-h-[60px] font-mono text-xs"
                />
              </>
            )}

            {verifyResults.new && <VerifyBadge result={verifyResults.new} />}

            <div className="flex items-center justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="mcp-add-verify"
                disabled={!draftInput || verifying === "new"}
                onClick={() => {
                  if (draftInput) void runVerify("new", () => verifyMcpServer(draftInput));
                }}
              >
                {verifying === "new" ? (
                  <Loader2Icon className="size-3.5 animate-spin" />
                ) : (
                  <PlugIcon className="size-3.5" />
                )}
                Verify
              </Button>
              <Button
                type="button"
                size="sm"
                data-testid="mcp-add-save"
                disabled={!draftInput || createMutation.isPending}
                onClick={() => {
                  if (draftInput) createMutation.mutate(draftInput);
                }}
              >
                Add server
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** A row for one existing MCP server with verify + delete controls. */
function ServerRow({
  server,
  verifyResult,
  verifying,
  onVerify,
  onDelete,
  deleting,
}: {
  server: McpServerObject;
  verifyResult: McpVerifyResult | undefined;
  verifying: boolean;
  onVerify: () => void;
  onDelete: () => void;
  deleting: boolean;
}) {
  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-border p-3"
      data-testid="mcp-server-row"
    >
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{server.name}</span>
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {server.transport}
            </span>
          </div>
          <p className="truncate text-xs text-muted-foreground">
            {server.transport === "http"
              ? server.url
              : [server.command, ...server.args].filter(Boolean).join(" ")}
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="mcp-server-verify"
          disabled={verifying}
          onClick={onVerify}
        >
          {verifying ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlugIcon className="size-3.5" />
          )}
          Verify
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          data-testid="mcp-server-delete"
          disabled={deleting}
          onClick={onDelete}
          className="size-7 text-muted-foreground hover:text-destructive"
        >
          <TrashIcon className="size-3.5" />
        </Button>
      </div>
      {verifyResult && <VerifyBadge result={verifyResult} />}
    </div>
  );
}

/** Inline connection-status badge: error message or the discovered tools. */
function VerifyBadge({ result }: { result: McpVerifyResult }) {
  if (!result.ok) {
    return (
      <div className="flex items-start gap-1.5 rounded bg-destructive/10 p-2 text-xs text-destructive">
        <XCircleIcon className="mt-0.5 size-3.5 shrink-0" />
        <span className="break-words">{result.error ?? "Connection failed"}</span>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 rounded bg-emerald-500/10 p-2 text-xs">
      <div className="flex items-center gap-1.5 font-medium text-emerald-600 dark:text-emerald-400">
        <CheckCircle2Icon className="size-3.5" />
        Connected — {result.tools.length} tool{result.tools.length === 1 ? "" : "s"}
      </div>
      {result.tools.length > 0 && (
        <ul className="flex flex-col gap-0.5 pl-5 text-muted-foreground">
          {result.tools.map((tool) => (
            <li key={tool.name} className="truncate">
              <span className="font-mono text-foreground">{tool.name}</span>
              {tool.description ? ` — ${tool.description}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
