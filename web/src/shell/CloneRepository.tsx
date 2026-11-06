import { type KeyboardEvent, useEffect, useState } from "react";
import { FolderOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
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
  type ClonedHostRepository,
  readLastCloneParent,
  useCloneHostRepository,
} from "@/hooks/useCloneHostRepository";
import { deriveRepoName, isValidSandboxRepoUrl } from "@/lib/sandboxRepositories";
import { isNavigablePath, joinPath } from "./WorkspacePicker";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

/**
 * Where a clone of ``url`` into ``parent`` will land, for the form preview.
 *
 * @param url Repository URL typed so far.
 * @param parent Destination parent folder.
 * @returns The checkout path, e.g. ``"~/code/repo"``, or ``null`` while
 *   either input is unusable.
 */
export function cloneDestination(url: string, parent: string): string | null {
  const name = isValidSandboxRepoUrl(url) ? deriveRepoName(url) : null;
  const trimmedParent = parent.trim();
  if (name === null || !isNavigablePath(trimmedParent)) return null;
  return joinPath(trimmedParent, name);
}

/**
 * Helper text under the destination field.
 *
 * @param parentInvalid Whether the parent folder is unusable.
 * @param destination Checkout path from {@link cloneDestination}, or ``null``.
 * @returns The hint to show.
 */
function destinationHint(parentInvalid: boolean, destination: string | null): string {
  if (parentInvalid) return "Use an absolute folder or one under ~.";
  if (destination) return `Clones into ${destination} using this host's git credentials.`;
  return "Uses this host's git credentials, such as its SSH keys.";
}

export interface CloneRepositoryFieldsProps {
  /** Host whose folders the Browse button lists; ``null`` disables Browse. */
  hostId: string | null;
  url: string;
  onUrlChange: (url: string) => void;
  parentDirectory: string;
  onParentDirectoryChange: (parent: string) => void;
  /** Submit the surrounding form, e.g. when Enter is pressed in a field. */
  onSubmit?: () => void;
  disabled?: boolean;
  autoFocus?: boolean;
}

/** Repository URL and destination fields shared by every clone entry point. */
export function CloneRepositoryFields({
  hostId,
  url,
  onUrlChange,
  parentDirectory,
  onParentDirectoryChange,
  onSubmit,
  disabled = false,
  autoFocus = false,
}: CloneRepositoryFieldsProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const urlInvalid = url.trim() !== "" && !isValidSandboxRepoUrl(url);
  const parentInvalid = !isNavigablePath(parentDirectory.trim());
  const destination = cloneDestination(url, parentDirectory);
  const submitOnEnter = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter" || !onSubmit) return;
    event.preventDefault();
    onSubmit();
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="clone-repository-url" className="text-sm font-medium text-muted-foreground">
          Repository URL
        </label>
        <Input
          id="clone-repository-url"
          data-testid="clone-repository-url"
          autoFocus={autoFocus}
          autoComplete="off"
          spellCheck={false}
          placeholder="https://github.com/org/repo or git@github.com:org/repo.git"
          value={url}
          disabled={disabled}
          aria-invalid={urlInvalid || undefined}
          onChange={(event) => onUrlChange(event.target.value)}
          onKeyDown={submitOnEnter}
        />
        {urlInvalid && (
          <p className="text-sm text-destructive" data-testid="clone-repository-url-error">
            Enter an https:// or git@ URL that ends in the repository name, without a token in it.
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        <label
          htmlFor="clone-repository-parent"
          className="text-sm font-medium text-muted-foreground"
        >
          Clone into
        </label>
        <div className="flex gap-2">
          <Input
            id="clone-repository-parent"
            data-testid="clone-repository-parent"
            autoComplete="off"
            spellCheck={false}
            value={parentDirectory}
            disabled={disabled}
            aria-invalid={parentInvalid || undefined}
            onChange={(event) => onParentDirectoryChange(event.target.value)}
            onKeyDown={submitOnEnter}
          />
          <Button
            type="button"
            variant="outline"
            data-testid="clone-repository-browse"
            disabled={disabled || hostId === null}
            onClick={() => setPickerOpen(true)}
          >
            <FolderOpenIcon className="size-4" />
            Browse
          </Button>
        </div>
        <p className="text-sm text-muted-foreground" data-testid="clone-repository-destination">
          {destinationHint(parentInvalid, destination)}
        </p>
      </div>
      <WorkspacePickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        hostId={hostId}
        initialPath={isNavigablePath(parentDirectory.trim()) ? parentDirectory.trim() : undefined}
        title="Choose where to clone"
        description="Choose the folder that will hold the repository."
        onConfirm={onParentDirectoryChange}
      />
    </div>
  );
}

export interface CloneRepositoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Host to clone onto. */
  hostId: string | null;
  onCloned: (clone: ClonedHostRepository) => void;
}

/** Dialog that clones a repository onto the selected host for the composer. */
export function CloneRepositoryDialog({
  open,
  onOpenChange,
  hostId,
  onCloned,
}: CloneRepositoryDialogProps) {
  const cloneRepository = useCloneHostRepository();
  const { reset } = cloneRepository;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Closing mid-clone would hide the result of a clone that keeps running.
        if (!next && cloneRepository.isPending) return;
        onOpenChange(next);
      }}
    >
      <DialogContent data-testid="clone-repository-dialog">
        <DialogHeader>
          <DialogTitle>Clone repository</DialogTitle>
          <DialogDescription>
            Clone a git repository onto this host and use it as the working directory.
          </DialogDescription>
        </DialogHeader>
        {/* Mounted per opening, so each one starts from a blank URL. */}
        <CloneRepositoryForm
          hostId={hostId}
          cloneRepository={cloneRepository}
          onCancel={() => onOpenChange(false)}
          onCloned={(clone) => {
            onCloned(clone);
            onOpenChange(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function CloneRepositoryForm({
  hostId,
  cloneRepository,
  onCancel,
  onCloned,
}: {
  hostId: string | null;
  cloneRepository: ReturnType<typeof useCloneHostRepository>;
  onCancel: () => void;
  onCloned: (clone: ClonedHostRepository) => void;
}) {
  const [url, setUrl] = useState("");
  const [parentDirectory, setParentDirectory] = useState(() => readLastCloneParent(hostId));
  const ready =
    hostId !== null &&
    cloneDestination(url, parentDirectory) !== null &&
    !cloneRepository.isPending;
  const submit = () => {
    if (!ready || hostId === null) return;
    cloneRepository.mutate({ hostId, url, parentDirectory }, { onSuccess: onCloned });
  };

  return (
    <>
      <CloneRepositoryFields
        hostId={hostId}
        url={url}
        onUrlChange={setUrl}
        parentDirectory={parentDirectory}
        onParentDirectoryChange={setParentDirectory}
        onSubmit={submit}
        disabled={cloneRepository.isPending}
        autoFocus
      />
      {cloneRepository.isError && (
        <p
          className="text-sm break-words text-destructive"
          role="alert"
          data-testid="clone-repository-error"
        >
          {cloneRepository.error.message}
        </p>
      )}
      <DialogFooter className="border-t-0 bg-transparent">
        <Button
          type="button"
          variant="ghost"
          disabled={cloneRepository.isPending}
          onClick={onCancel}
        >
          Cancel
        </Button>
        <Button
          type="button"
          data-testid="clone-repository-confirm"
          loading={cloneRepository.isPending}
          disabled={!ready}
          onClick={submit}
        >
          {cloneRepository.isPending ? "Cloning…" : "Clone"}
        </Button>
      </DialogFooter>
    </>
  );
}
