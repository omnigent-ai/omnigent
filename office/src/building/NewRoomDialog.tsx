import { useEffect, useRef, useState } from "react";
import type { BuildingSnapshot } from "../../shared/types.ts";
import { officeApi } from "../api.ts";

interface NewRoomDialogProps {
  onClose: () => void;
  onCreated: (snapshot: BuildingSnapshot) => void;
}

/** Creates an Omnigent project; it appears as a new, unthemed room. */
export function NewRoomDialog({ onClose, onCreated }: NewRoomDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      onCreated(await officeApi.createRoom(name.trim(), icon.trim()));
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
        <p className="muted small">Creates a project on the connected Omnigent. Assign a theme from inside the room.</p>
        <label>
          Project name <input required autoFocus value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Icon (one emoji, optional) <input value={icon} maxLength={8} onChange={(event) => setIcon(event.target.value)} />
        </label>
        {error ? (
          <p className="error-text" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            Create room
          </button>
        </div>
      </form>
    </dialog>
  );
}
