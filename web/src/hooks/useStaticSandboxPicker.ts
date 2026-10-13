import { useRef, useState } from "react";
import { showToast } from "@/components/ui/toast";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  useRemoveStaticSandbox,
  useStaticSandboxes,
  wakeStaticSandbox,
} from "@/lib/staticSandboxesApi";
import { type StaticSandbox, soleStaticSandboxProvider } from "@/lib/staticSandboxes";
import { useWakeStaticSandboxOnInput } from "./useWakeOnFirstInput";

// How long a static sandbox create runs before the composer says the
// host is still starting.
export const STATIC_CREATE_SLOW_MS = 8_000;

/**
 * The ``workspace`` for a static sandbox create: the first picked repo
 * (``url#branch``), or omitted so the server uses the VM's primary repo. A
 * project create pins explicit nulls so config hints can't be default-filled.
 */
export function staticSandboxWorkspaceFields(
  workspaces: string[],
  projectCreate: boolean,
): { workspace?: string | null; git?: null } {
  if (workspaces.length > 0) return { workspace: workspaces[0] };
  return projectCreate ? { workspace: null, git: null } : {};
}

export interface UseStaticSandboxPickerOptions {
  /** The static sandbox to start on (a restored draft's pick), or null. */
  initialId: string | null;
  /** Whether the composer targets a sandbox; a static pick only counts then. */
  sandboxSelected: boolean;
  /** The composer text; the first keystroke wakes a paused pick. */
  message: string;
  /** Switches the composer to its sandbox target (runs before the pick is set). */
  onSelect: () => void;
}

export interface StaticSandboxPicker {
  /** Whether the server offers static sandboxes (GET /v1/info). */
  enabled: boolean;
  /** The server's only static sandbox provider, to name in copy; null when several. */
  provider: string | null;
  sandboxes: StaticSandbox[];
  /** The picked sandbox id, null for a new managed sandbox. */
  id: string | null;
  /** Stable across renders (a state setter). */
  setId: (id: string | null) => void;
  /** `id` while the composer targets a sandbox, else null: what a create uses. */
  activeId: string | null;
  /** The picked sandbox's label (its id until the list loads), or null. */
  selectedLabel: string | null;
  /** Picks a static sandbox as the user's own host choice. */
  select: (id: string) => void;
  remove: (id: string) => void;
  /** Opens a pending add/rename dialog once the host menu has closed. */
  onMenuOpenChange: (open: boolean) => void;
  requestAdd: () => void;
  /** Asks to rename `id`; the row's select then skips (see `selectFromMenu`). */
  requestRename: (id: string) => void;
  selectFromMenu: (id: string) => void;
  addOpen: boolean;
  setAddOpen: (open: boolean) => void;
  renaming: StaticSandbox | null;
  closeRename: () => void;
  /** Shows the create's progress toasts; call the result once it settles. */
  beginCreate: () => () => void;
  /**
   * Create-body fields for a static pick, spread after the managed sandbox
   * fields to replace them (undefined keys drop out of the JSON), else `{}`.
   */
  createTarget: (workspaces: string[], projectCreate: boolean) => Record<string, unknown>;
}

/** New Session's static sandbox picks: the host-menu rows, dialogs, and create fields. */
export function useStaticSandboxPicker({
  initialId,
  sandboxSelected,
  message,
  onSelect,
}: UseStaticSandboxPickerOptions): StaticSandboxPicker {
  const info = useServerInfo();
  const enabled = info !== "loading" && info.static_sandboxes === true;
  const provider =
    info !== "loading" ? soleStaticSandboxProvider(info.static_sandbox_providers) : null;
  const [id, setId] = useState<string | null>(initialId);
  const [addOpen, setAddOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const pendingAddRef = useRef(false);
  const pendingRenameRef = useRef<string | null>(null);
  const sandboxes = useStaticSandboxes({ enabled }).data ?? [];
  const removeMutation = useRemoveStaticSandbox();
  const selected = id !== null ? (sandboxes.find((t) => t.id === id) ?? null) : null;
  // Wake a paused VM on the first keystroke, so its ~30s resume overlaps typing.
  useWakeStaticSandboxOnInput(sandboxSelected ? selected : null, message, wakeStaticSandbox);
  const target = sandboxSelected ? id : null;

  function select(next: string) {
    onSelect();
    setId(next);
  }

  return {
    enabled,
    provider,
    sandboxes,
    id,
    setId,
    activeId: target,
    selectedLabel: id !== null ? (selected?.label ?? id) : null,
    select,
    remove: (removed) => {
      const entry = sandboxes.find((t) => t.id === removed);
      if (id === removed) setId(null);
      if (entry !== undefined) {
        removeMutation.mutate({ provider: entry.provider, sandboxId: removed });
      }
    },
    onMenuOpenChange: (open) => {
      if (open) return;
      if (pendingAddRef.current) {
        pendingAddRef.current = false;
        setAddOpen(true);
      }
      if (pendingRenameRef.current !== null) {
        setRenamingId(pendingRenameRef.current);
        pendingRenameRef.current = null;
      }
    },
    requestAdd: () => {
      pendingAddRef.current = true;
    },
    requestRename: (renamed) => {
      pendingRenameRef.current = renamed;
    },
    selectFromMenu: (picked) => {
      // The pencil asked to rename instead of select.
      if (pendingRenameRef.current === null) select(picked);
    },
    addOpen,
    setAddOpen,
    renaming: sandboxes.find((t) => t.id === renamingId) ?? null,
    closeRename: () => setRenamingId(null),
    beginCreate: () => {
      if (target === null) return () => undefined;
      const label = selected?.label || target;
      if (selected?.status === "paused") {
        showToast(`Waking ${label}… (~30s), then starting your session on it`);
      }
      // A slow create may be starting the host or cloning the repo; the create
      // can't say which while it waits, so the toast names neither.
      const timer = setTimeout(
        () => showToast(`Preparing ${label}… (up to 2 minutes the first time)`),
        STATIC_CREATE_SLOW_MS,
      );
      return () => clearTimeout(timer);
    },
    createTarget: (workspaces, projectCreate) =>
      target === null
        ? {}
        : {
            // The server wakes the VM, starts its host, and gives this session
            // its own worktree.
            host_type: "external",
            sandbox_target: {
              kind: "static",
              // Before the list loads, the server's only provider; with several, the
              // create is refused rather than guessed.
              provider: selected?.provider ?? provider ?? undefined,
              sandbox_id: target,
            },
            workspaces: undefined,
            sandbox_provider: undefined,
            inference_configuration_revision: undefined,
            workspace: undefined,
            git: undefined,
            ...staticSandboxWorkspaceFields(workspaces, projectCreate),
          },
  };
}
