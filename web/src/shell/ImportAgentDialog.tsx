import { useRef, useState } from "react";
import { CheckCircle2Icon, FileIcon, FolderIcon, Loader2Icon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { buildBundleFromFiles } from "@/lib/agentBundle";
import { validateAgentBundle } from "@/lib/sessionsApi";

/** A packed agent bundle ready to upload, with a display label. */
export interface ImportedAgent {
  /** Display label shown in the picker. */
  name: string;
  /** Pre-built `.tar.gz` bundle accepted by the multipart session create. */
  bundle: File;
}

/** Derive a display name from a picked file or the top folder of a pick. */
function deriveName(files: File[]): string {
  if (files.length === 0) return "";
  const rel = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath;
  if (rel) {
    const top = rel.split("/")[0];
    if (top) return top;
  }
  return files[0].name.replace(/\.ya?ml$/i, "");
}

/**
 * Dialog for importing a custom agent from disk — either a single agent
 * YAML file or a whole config folder (config.yaml + AGENTS.md + skills/…).
 *
 * The OS-native picker is triggered by hidden file inputs: a plain input
 * for a single YAML, and a `webkitdirectory` input for a folder. Both work
 * uniformly in the browser and inside the Electron shell's Chromium. On
 * submit, the picked files are packed verbatim into a `.tar.gz` and handed
 * back via `onImport` so the parent can start a session with it — the same
 * bundle path used for created agents.
 */
export function ImportAgentDialog({
  open,
  onOpenChange,
  onImport,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImport: (agent: ImportedAgent) => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  // Whether the user has edited the name; until then we keep it in sync
  // with the picked file/folder so a pick auto-fills a sensible label.
  const [nameEdited, setNameEdited] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [kind, setKind] = useState<"file" | "folder" | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The packed bundle, set once the pick validates against the server.
  // `null` until a pick validates; cleared on any re-pick. Import is gated
  // on this being non-null, so an invalid config can never be imported.
  const [validated, setValidated] = useState<File | null>(null);
  const [validating, setValidating] = useState(false);

  function reset() {
    setName("");
    setNameEdited(false);
    setFiles([]);
    setKind(null);
    setError(null);
    setValidated(null);
    setValidating(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (folderInputRef.current) folderInputRef.current.value = "";
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  // Pack the pick and validate it server-side (the same check `omni run`
  // and the create path run), so a bad config.yaml is caught here — before
  // the user submits — instead of failing mid session-create.
  async function validatePick(list: File[], pickedKind: "file" | "folder") {
    setValidating(true);
    setError(null);
    setValidated(null);
    try {
      const bundle = await buildBundleFromFiles(list, {
        singleFileAsConfig: pickedKind === "file",
      });
      await validateAgentBundle(bundle);
      setValidated(bundle);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The selected agent config is not valid.");
    } finally {
      setValidating(false);
    }
  }

  function handlePicked(picked: FileList | null, pickedKind: "file" | "folder") {
    const list = picked ? Array.from(picked) : [];
    if (list.length === 0) return;
    setFiles(list);
    setKind(pickedKind);
    if (!nameEdited) setName(deriveName(list));
    void validatePick(list, pickedKind);
  }

  function handleImport() {
    // Import is gated on a validated bundle, so this is the packed, verified
    // one — no re-packing or re-validation needed.
    if (!validated) return;
    onImport({ name: name.trim() || deriveName(files), bundle: validated });
    reset();
    onOpenChange(false);
  }

  const fileCount = files.length;
  const canImport = validated !== null && !validating;
  const selectionLabel =
    kind === "folder"
      ? `${deriveName(files)} — ${fileCount} file${fileCount === 1 ? "" : "s"}`
      : (files[0]?.name ?? null);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="import-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Import agent config</DialogTitle>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          <p className="text-xs text-muted-foreground">
            Import an existing agent from a single config file (a YAML spec) or a whole config
            folder (<code>config.yaml</code>, <code>AGENTS.md</code>, <code>skills/</code>…). Files
            are packaged and uploaded as-is.
          </p>

          {/* Hidden native pickers. `webkitdirectory` selects a folder; the
          plain input restricts to YAML specs. */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".yaml,.yml"
            className="hidden"
            data-testid="import-agent-file-input"
            onChange={(e) => handlePicked(e.target.files, "file")}
          />
          <input
            ref={folderInputRef}
            type="file"
            className="hidden"
            data-testid="import-agent-folder-input"
            // webkitdirectory isn't in the standard input typings.
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
            onChange={(e) => handlePicked(e.target.files, "folder")}
          />

          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              className="flex-1 gap-2"
              data-testid="import-agent-choose-file"
              onClick={() => fileInputRef.current?.click()}
            >
              <FileIcon className="size-4" />
              Choose Config File
            </Button>
            <Button
              type="button"
              variant="outline"
              className="flex-1 gap-2"
              data-testid="import-agent-choose-folder"
              onClick={() => folderInputRef.current?.click()}
            >
              <FolderIcon className="size-4" />
              Choose Config Folder
            </Button>
          </div>

          {selectionLabel && (
            <div
              className="truncate rounded-md border border-border px-3 py-2 text-xs text-muted-foreground"
              data-testid="import-agent-selection"
            >
              {selectionLabel}
            </div>
          )}

          {validating && (
            <p
              className="flex items-center gap-1.5 text-xs text-muted-foreground"
              data-testid="import-agent-validating"
            >
              <Loader2Icon className="size-3.5 animate-spin" />
              Validating agent config…
            </p>
          )}

          {validated && !validating && (
            <p
              className="flex items-center gap-1.5 text-xs text-green-600 dark:text-green-500"
              data-testid="import-agent-validated"
            >
              <CheckCircle2Icon className="size-3.5" />
              Agent config is loaded.
            </p>
          )}

          {/* Display name — a label for the picker; the bundle's own
          config.yaml name is what the server registers. */}
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="import-agent-name"
              className="text-xs font-medium text-muted-foreground"
            >
              Display name
            </label>
            <Input
              id="import-agent-name"
              data-testid="import-agent-name"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setNameEdited(true);
              }}
              placeholder="my-agent"
            />
          </div>

          {error && (
            <p className="text-xs text-destructive" data-testid="import-agent-error">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button data-testid="import-agent-submit" onClick={handleImport} disabled={!canImport}>
            {validating ? "Validating…" : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
