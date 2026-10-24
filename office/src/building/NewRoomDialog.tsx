import { useEffect, useRef, useState } from "react";
import type { BuildingSnapshot } from "../../shared/types.ts";
import { officeApi } from "../api.ts";
import { HostFolderFields } from "../HostFolderFields.tsx";
import { SdlcFields, emptySdlcDraft, sdlcRuntime } from "../room/workflow/SdlcFields.tsx";

interface NewRoomDialogProps {
  onClose: () => void;
  onCreated: (snapshot: BuildingSnapshot) => void;
}

type ThemeChoice = "none" | "sdlc";

/** Creates an Omnigent project with its folder, optionally starting the SDLC theme in the same step. */
export function NewRoomDialog({ onClose, onCreated }: NewRoomDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState("");
  const [hostId, setHostId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [theme, setTheme] = useState<ThemeChoice>("none");
  const [draft, setDraft] = useState(emptySdlcDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const sdlcReady = theme === "none" || (Boolean(draft.harness) && (Boolean(hostId) || Boolean(draft.runnerId.trim())));

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await officeApi.createRoom({
        name: name.trim(),
        icon: icon.trim(),
        hostId: hostId || undefined,
        workspace: workspace.trim() || undefined,
        theme: theme === "sdlc" ? { runtime: sdlcRuntime(draft), gates: draft.gates } : null,
      });
      onCreated(result.snapshot);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="approval-dialog"
      aria-labelledby="new-room-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <h2 id="new-room-title">New room</h2>
        <p className="muted small">Creates a project on the connected Omnigent. Its host and folder become the project's session defaults.</p>
        <label>
          Project name <input required autoFocus maxLength={100} value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Icon (one emoji, optional) <input value={icon} maxLength={8} onChange={(event) => setIcon(event.target.value)} />
        </label>
        <HostFolderFields hostId={hostId} workspace={workspace} onHostChange={setHostId} onWorkspaceChange={setWorkspace} />
        <label>
          Theme{" "}
          <select value={theme} onChange={(event) => setTheme(event.target.value as ThemeChoice)}>
            <option value="none">None (generalist room)</option>
            <option value="sdlc">SDLC</option>
          </select>
        </label>
        {theme === "sdlc" ? <SdlcFields draft={draft} onChange={setDraft} /> : null}
        {error ? (
          <p className="error-text" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy || !name.trim() || !sdlcReady}>
            {busy && theme === "sdlc" ? "Starting the PM…" : "Create room"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
