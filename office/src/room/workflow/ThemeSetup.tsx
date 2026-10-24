import { useState } from "react";
import { ROLES } from "../../../shared/sdlcTheme.ts";
import type { RoomSummary } from "../../../shared/types.ts";
import { officeApi } from "../../api.ts";
import { HostFolderFields } from "../../HostFolderFields.tsx";
import { SdlcFields, emptySdlcDraft, sdlcRuntime } from "./SdlcFields.tsx";

interface ThemeSetupProps {
  room: RoomSummary;
  onAssigned: () => void;
}

export function ThemeSetup({ room, onAssigned }: ThemeSetupProps) {
  const [draft, setDraft] = useState(emptySdlcDraft);
  const [hostId, setHostId] = useState(room.hostId ?? "");
  const [workspace, setWorkspace] = useState(room.workspace ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const runtime = { ...sdlcRuntime(draft), ...(hostId ? { hostId } : {}), ...(workspace.trim() ? { workspace: workspace.trim() } : {}) };
    try {
      await officeApi.assignTheme(room.projectId, runtime, draft.gates);
      onAssigned();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="theme-setup" aria-label="Assign a theme">
      <h2>Assign the SDLC theme</h2>
      <p className="muted">
        A PM coordinator starts now; {ROLES.filter((role) => !role.human && role.id !== "pm").map((role) => role.title).join(", ")} join
        when the PM first dispatches work to them. You are the Product Owner. Existing sessions stay as the unthemed generalist branch.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <SdlcFields draft={draft} onChange={setDraft} />
        <HostFolderFields hostId={hostId} workspace={workspace} onHostChange={setHostId} onWorkspaceChange={setWorkspace} />
        {error ? (
          <p className="error-text" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="primary" disabled={busy || !draft.harness || (!hostId && !draft.runnerId.trim())}>
          {busy ? "Starting the PM…" : "Assign SDLC"}
        </button>
      </form>
    </section>
  );
}
