import { useState } from "react";
import { CheckIcon, ServerIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { sandboxProviderName } from "@/lib/capabilities";
import { cn } from "@/lib/utils";
import {
  STATIC_SANDBOX_LABEL_MAX_LENGTH,
  type StaticSandbox,
  type AvailableSandbox,
  staticSandboxLastActiveText,
  staticSandboxStatusLabel,
  normalizeStaticSandboxLabel,
} from "@/lib/staticSandboxes";
import {
  useAvailableSandboxes,
  useStaticSandboxes,
  useSaveStaticSandbox,
} from "@/lib/staticSandboxesApi";

export function StaticSandboxStatusBadge({ status }: { status: AvailableSandbox["status"] }) {
  const styles = {
    running: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
    paused: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
    gone: "bg-destructive/10 text-destructive",
  }[status];
  // A paused sandbox wakes on first use.
  const label = staticSandboxStatusLabel(status);
  return (
    <span
      className={cn("rounded-full px-1.5 py-px text-[11px] font-medium leading-4", styles)}
      data-testid={`static-sandbox-status-${status}`}
    >
      {label}
    </span>
  );
}

function vmDetail(vm: AvailableSandbox): string {
  const active = staticSandboxLastActiveText(vm.lastActivityAt);
  return [vm.repo, active !== null ? `active ${active}` : ""].filter(Boolean).join(" · ");
}

function entryKey(vm: AvailableSandbox): string {
  return `${vm.provider}/${vm.id}`;
}

// Lists the user's sandboxes at each provider and adds the pick as a static sandbox.
export function AddStaticSandboxDialog({
  open,
  onOpenChange,
  onAdded,
  provider = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: (id: string) => void;
  /** The provider to name in the copy, or null to name none. */
  provider?: string | null;
}) {
  const available = useAvailableSandboxes({ enabled: open });
  const vms = (available.data ?? []).filter((vm) => vm.status !== "gone");
  const saved = useStaticSandboxes({ enabled: open }).data ?? [];
  const savedKeys = new Set(saved.map(entryKey));
  const save = useSaveStaticSandbox();
  const [picked, setPicked] = useState<AvailableSandbox | null>(null);
  const [label, setLabel] = useState("");
  // Trailing space included, so copy reads "your VMs" when no provider is named.
  const providerName = provider !== null ? `${sandboxProviderName(provider)} ` : "";

  function pick(vm: AvailableSandbox) {
    setPicked(vm);
    setLabel(vm.id);
  }

  function add() {
    if (picked === null || save.isPending) return;
    const sandboxId = picked.id;
    save.mutate(
      {
        provider: picked.provider,
        sandboxId,
        label: normalizeStaticSandboxLabel(label, sandboxId),
      },
      {
        onSuccess: () => {
          onAdded(sandboxId);
          setPicked(null);
          setLabel("");
          onOpenChange(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Opened from the host menu: its close returns focus to the trigger,
          which must not dismiss this dialog. */}
      <DialogContent
        className="max-h-[85vh] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-lg"
        data-testid="add-static-sandbox-dialog"
        onFocusOutside={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{`Add a ${providerName}Static Sandbox`}</DialogTitle>
          <DialogDescription>
            {`Pick one of your ${providerName}VMs. Every session on this sandbox runs on that VM, and Omnigent never deletes it.`}
          </DialogDescription>
        </DialogHeader>
        {/* The VM list and label field scroll; the header and footer stay put. */}
        <div
          className="-mx-2 flex min-h-0 flex-col gap-4 overflow-y-auto px-2"
          data-testid="add-static-sandbox-body"
        >
          {available.isPending && (
            <p className="text-sm text-muted-foreground">Loading your VMs…</p>
          )}
          {available.isError && (
            <p className="text-sm text-destructive" data-testid="add-static-sandbox-error">
              {available.error.message}
            </p>
          )}
          <div
            className="flex flex-col gap-1"
            role="listbox"
            aria-label={`Your ${providerName}VMs`}
          >
            {vms.map((vm) => {
              const alreadySaved = savedKeys.has(entryKey(vm));
              const isPicked = picked !== null && entryKey(picked) === entryKey(vm);
              return (
                <button
                  key={entryKey(vm)}
                  type="button"
                  role="option"
                  aria-selected={isPicked}
                  disabled={alreadySaved}
                  onClick={() => pick(vm)}
                  data-testid={`add-static-sandbox-option-${vm.id}`}
                  className={cn(
                    "flex items-center gap-3 rounded-xl border border-transparent px-3 py-2 text-left hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50",
                    isPicked && "border-border bg-muted",
                  )}
                >
                  <ServerIcon className="size-4 shrink-0 text-muted-foreground" />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-medium">
                      <span className="min-w-0 break-all">{vm.id}</span>
                      <StaticSandboxStatusBadge status={vm.status} />
                    </span>
                    <span className="text-xs break-words text-muted-foreground">
                      {vmDetail(vm)}
                    </span>
                  </span>
                  {alreadySaved ? (
                    <span className="text-xs text-muted-foreground">Added</span>
                  ) : (
                    isPicked && <CheckIcon className="size-4" />
                  )}
                </button>
              );
            })}
          </div>
          {picked !== null && (
            <StaticSandboxLabelField
              id="add-static-sandbox-label"
              value={label}
              onChange={setLabel}
              onSubmit={add}
            />
          )}
          {save.isError && (
            <p className="text-sm text-destructive" data-testid="add-static-sandbox-save-error">
              {save.error.message}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Don't see your VM? Create it with your sandbox provider, then reopen this dialog.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={add}
            disabled={picked === null || save.isPending}
            data-testid="add-static-sandbox-confirm"
          >
            Add
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StaticSandboxLabelField({
  id,
  value,
  onChange,
  onSubmit,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        Label in the host menu
      </label>
      <Input
        id={id}
        value={value}
        maxLength={STATIC_SANDBOX_LABEL_MAX_LENGTH}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSubmit();
        }}
        data-testid={id}
      />
    </div>
  );
}

// Renames a static sandbox's label; the VM itself is unchanged.
export function RenameStaticSandboxDialog({
  target,
  onOpenChange,
}: {
  target: StaticSandbox | null;
  onOpenChange: (open: boolean) => void;
}) {
  const [label, setLabel] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const rename = useSaveStaticSandbox();
  const providerName = target !== null ? sandboxProviderName(target.provider) : "";
  if (target !== null && editing !== target.id) {
    setEditing(target.id);
    setLabel(target.label);
  }

  function save() {
    if (target === null || rename.isPending) return;
    rename.mutate(
      {
        provider: target.provider,
        sandboxId: target.id,
        label: normalizeStaticSandboxLabel(label, target.id),
      },
      { onSuccess: () => onOpenChange(false) },
    );
  }

  return (
    <Dialog
      open={target !== null}
      onOpenChange={(open) => {
        if (!open) setEditing(null);
        onOpenChange(open);
      }}
    >
      <DialogContent
        className="sm:max-w-sm"
        data-testid="rename-static-sandbox-dialog"
        onFocusOutside={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{`Rename ${providerName} Static Sandbox`}</DialogTitle>
          <DialogDescription>
            {`The ${providerName} VM `}
            <code>{target?.id}</code> keeps its name; only the label changes.
          </DialogDescription>
        </DialogHeader>
        <StaticSandboxLabelField
          id="rename-static-sandbox-label"
          value={label}
          onChange={setLabel}
          onSubmit={save}
        />
        {rename.isError && <p className="text-sm text-destructive">{rename.error.message}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={save}
            disabled={rename.isPending}
            data-testid="rename-static-sandbox-save"
          >
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
