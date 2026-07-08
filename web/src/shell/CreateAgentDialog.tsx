import { useRef, useState } from "react";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BRAIN_HARNESS_LABELS, useBrainHarnessLabels } from "@/lib/agentLabels";
import {
  buildBundleFromFiles,
  type BundleFile,
  type MCPServerInput,
  type PendingAgentSource,
} from "@/lib/agentBundle";

/**
 * Harness options for the picker. "default" uses the server's default
 * executor (no explicit harness in the bundle).
 */
const DEFAULT_HARNESS = Object.keys(BRAIN_HARNESS_LABELS)[0];

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

/** Derive a display name from a bundle path or filename. */
function bundleDisplayName(path: string): string {
  const base = path.split("/")[0] || path;
  return base.replace(/\.(tar\.gz|ya?ml)$/i, "") || "custom-agent";
}

/** Derive a display name from a GitHub URL / shorthand (the "repo" segment). */
function githubDisplayName(url: string): string {
  const cleaned = url.replace(/^https?:\/\/github\.com\//i, "").replace(/\.git$/i, "");
  const match = cleaned.match(/^[^/]+\/([^/@]+)/);
  return match?.[1] ?? "github-agent";
}

/**
 * Dialog for creating a custom agent from the new-session picker.
 *
 * Four sources (tabs): a field Form, a File upload (single YAML or a
 * pre-built `.tar.gz`), a Folder upload (a whole bundle directory), and a
 * public GitHub repo URL. Each produces a {@link PendingAgentSource} passed
 * back via `onCreate`; the parent builds/uploads the bundle and starts a
 * session. Browser uploads keep `${VAR}` references literal (no env), so
 * secrets must never be committed to a folder or public repo.
 */
export function CreateAgentDialog({
  open,
  onOpenChange,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (source: PendingAgentSource) => void;
}) {
  const brainHarnessLabels = useBrainHarnessLabels();
  const harnessOptions = Object.entries(brainHarnessLabels).map(([value, label]) => ({
    value,
    label,
  }));
  const [tab, setTab] = useState<"form" | "file" | "folder" | "github">("form");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [harness, setHarness] = useState(DEFAULT_HARNESS);
  const [model, setModel] = useState("");
  const [mcpEntries, setMcpEntries] = useState<MCPFormEntry[]>([]);
  const [nextKey, setNextKey] = useState(0);

  // Non-form tab state.
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [githubUrl, setGithubUrl] = useState("");
  const [githubRef, setGithubRef] = useState("");
  const [githubSubdir, setGithubSubdir] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  function reset() {
    setTab("form");
    setName("");
    setDescription("");
    setInstructions("");
    setHarness(DEFAULT_HARNESS);
    setModel("");
    setMcpEntries([]);
    setNextKey(0);
    setUploadError(null);
    setBusy(false);
    setGithubUrl("");
    setGithubRef("");
    setGithubSubdir("");
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

  /** Emit a source and close the dialog. */
  function emit(source: PendingAgentSource) {
    onCreate(source);
    reset();
    onOpenChange(false);
  }

  function handleSubmit() {
    const trimmedName = name.trim();
    if (!trimmedName) return;
    emit({
      kind: "form",
      input: {
        name: trimmedName,
        description: description.trim() || undefined,
        instructions: instructions.trim() || undefined,
        harness,
        model: model.trim(),
        mcpServers: toMCPInputs(mcpEntries),
      },
    });
  }

  /** Read selected files (single YAML, folder, or tarball) into a bundle. */
  async function handleFiles(files: FileList | null, kind: "file" | "folder") {
    setUploadError(null);
    if (!files || files.length === 0) return;
    setBusy(true);
    try {
      // A single .tar.gz is already a bundle — pass it straight through.
      if (kind === "file" && files.length === 1 && /\.tar\.gz$/i.test(files[0].name)) {
        emit({ kind: "bundle", bundle: files[0], name: bundleDisplayName(files[0].name) });
        return;
      }
      const bundleFiles: BundleFile[] = await Promise.all(
        Array.from(files).map(async (file) => ({
          // Folder pick carries webkitRelativePath (e.g. "my-agent/config.yaml");
          // a single file has none, so fall back to its name.
          path: file.webkitRelativePath || file.name,
          bytes: new Uint8Array(await file.arrayBuffer()),
        })),
      );
      const bundle = await buildBundleFromFiles(bundleFiles);
      emit({ kind: "bundle", bundle, name: bundleDisplayName(bundleFiles[0]?.path ?? "agent") });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function handleGithubSubmit() {
    const url = githubUrl.trim();
    if (!url) return;
    emit({
      kind: "github",
      sourceUrl: url,
      ref: githubRef.trim() || undefined,
      subdir: githubSubdir.trim() || undefined,
      name: githubDisplayName(url),
    });
  }

  const canSubmit = name.trim().length > 0 && model.trim().length > 0;
  const canSubmitGithub = githubUrl.trim().length > 0 && !busy;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="create-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Create custom agent</DialogTitle>
        </DialogHeader>

        <Tabs
          value={tab}
          onValueChange={(v) => {
            setUploadError(null);
            setTab(v as typeof tab);
          }}
          className="flex min-h-0 flex-1 flex-col gap-4"
        >
          <TabsList className="w-full" data-testid="create-agent-tabs">
            <TabsTrigger value="form" data-testid="create-agent-tab-form">
              Form
            </TabsTrigger>
            <TabsTrigger value="file" data-testid="create-agent-tab-file">
              File
            </TabsTrigger>
            <TabsTrigger value="folder" data-testid="create-agent-tab-folder">
              Folder
            </TabsTrigger>
            <TabsTrigger value="github" data-testid="create-agent-tab-github">
              GitHub
            </TabsTrigger>
          </TabsList>

          <TabsContent value="form" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
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
          </TabsContent>

          {/* File tab — a single YAML or a pre-built .tar.gz bundle. */}
          <TabsContent value="file" className="flex flex-col gap-3">
            <p className="text-13 text-muted-foreground">
              Upload a single <code>config.yaml</code> / <code>agent.yaml</code>, or a pre-built{" "}
              <code>.tar.gz</code> bundle.
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept=".yaml,.yml,.tar.gz"
              className="hidden"
              data-testid="create-agent-file-input"
              onChange={(e) => void handleFiles(e.target.files, "file")}
            />
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
              data-testid="create-agent-file-pick"
            >
              Choose file…
            </Button>
            <VarNote />
          </TabsContent>

          {/* Folder tab — a whole bundle directory (webkitdirectory). */}
          <TabsContent value="folder" className="flex flex-col gap-3">
            <p className="text-13 text-muted-foreground">
              Upload a folder containing <code>config.yaml</code> at its root, plus any bundled
              instructions, MCP configs, or skills.
            </p>
            <input
              ref={folderInputRef}
              type="file"
              // webkitdirectory is a non-standard attribute React doesn't type.
              {...{ webkitdirectory: "", directory: "" }}
              multiple
              className="hidden"
              data-testid="create-agent-folder-input"
              onChange={(e) => void handleFiles(e.target.files, "folder")}
            />
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => folderInputRef.current?.click()}
              data-testid="create-agent-folder-pick"
            >
              Choose folder…
            </Button>
            <VarNote />
          </TabsContent>

          {/* GitHub tab — a public repo URL, fetched + repackaged server-side. */}
          <TabsContent value="github" className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="create-agent-github-url"
                className="text-xs font-medium text-muted-foreground"
              >
                Public repo URL <span className="text-destructive">*</span>
              </label>
              <Input
                id="create-agent-github-url"
                data-testid="create-agent-github-url"
                value={githubUrl}
                onChange={(e) => setGithubUrl(e.target.value)}
                placeholder="https://github.com/owner/repo"
                autoFocus
              />
            </div>
            <div className="flex gap-2">
              <div className="flex flex-1 flex-col gap-1.5">
                <label
                  htmlFor="create-agent-github-ref"
                  className="text-xs font-medium text-muted-foreground"
                >
                  Ref (tag / branch / commit)
                </label>
                <Input
                  id="create-agent-github-ref"
                  data-testid="create-agent-github-ref"
                  value={githubRef}
                  onChange={(e) => setGithubRef(e.target.value)}
                  placeholder="v1.0.0"
                />
              </div>
              <div className="flex flex-1 flex-col gap-1.5">
                <label
                  htmlFor="create-agent-github-subdir"
                  className="text-xs font-medium text-muted-foreground"
                >
                  Subdir
                </label>
                <Input
                  id="create-agent-github-subdir"
                  data-testid="create-agent-github-subdir"
                  value={githubSubdir}
                  onChange={(e) => setGithubSubdir(e.target.value)}
                  placeholder="agents/my-agent"
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Pin a ref for reproducibility; the default branch is used otherwise. Public repos
              only.
            </p>
            <VarNote />
          </TabsContent>
        </Tabs>

        {uploadError && (
          <p className="text-13 text-destructive" data-testid="create-agent-upload-error">
            {uploadError}
          </p>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          {tab === "form" && (
            <Button data-testid="create-agent-submit" onClick={handleSubmit} disabled={!canSubmit}>
              Create
            </Button>
          )}
          {tab === "github" && (
            <Button
              data-testid="create-agent-github-submit"
              onClick={handleGithubSubmit}
              disabled={!canSubmitGithub}
            >
              Create from GitHub
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Inline note reminding users that `${VAR}` refs are uploaded literally. */
function VarNote() {
  return (
    <p className="text-xs text-muted-foreground/80">
      <code>{"${VAR}"}</code> references are uploaded as-is and never expanded — keep secrets out of
      the bundle.
    </p>
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
