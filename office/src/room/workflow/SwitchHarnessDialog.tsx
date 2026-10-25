import { useEffect, useRef, useState } from "react";
import { roleTitle, type AgentRoleId } from "../../../shared/sdlcTheme.ts";
import { READINESS_LABELS, harnessReadiness, type RoleHealth } from "../../../shared/troubleshoot.ts";
import { officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";

interface SwitchHarnessDialogProps {
  branchId: string;
  hostId: string | undefined;
  target: RoleHealth;
  /** Persistent roles in the branch, to offer moving every role off a failing harness. */
  roles: RoleHealth[];
  reason: string;
  onClose: () => void;
  onSwitched: (message: string) => void;
}

/** Move a role, or every role on the same harness, to another harness with a recorded handoff. */
export function SwitchHarnessDialog({ branchId, hostId, target, roles, reason, onClose, onSwitched }: SwitchHarnessDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const options = useRuntimeOptions();
  const harnesses = (options?.harnesses.data ?? []).filter((entry) => entry.id !== target.harness);
  const report = (options?.hosts.hosts ?? []).find((host) => host.host_id === hostId)?.configured_harnesses;
  const readiness = (harness: string) => (hostId ? harnessReadiness(harness, report) : "unknown");
  const firstReady = harnesses.find((entry) => readiness(entry.id) === "ready")?.id ?? harnesses[0]?.id ?? "";
  const [harness, setHarness] = useState(firstReady);
  const [model, setModel] = useState("");
  const sameHarness = [...new Set(roles.filter((entry) => !entry.temporary && entry.harness === target.harness).map((entry) => entry.role))];
  const [scope, setScope] = useState<"role" | "harness">("role");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const affected: AgentRoleId[] = scope === "harness" ? sameHarness : [target.role];
  const chosenReadiness = harness ? readiness(harness) : "unknown";

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await officeApi.switchHarness(branchId, affected, harness, model.trim(), reason);
      onSwitched(`${affected.map(roleTitle).join(", ")} moved to ${harness} and restarted with a handoff.`);
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
      aria-labelledby="switch-harness-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <h2 id="switch-harness-title">Switch the {roleTitle(target.role)} to another harness</h2>
        <p className="muted small">
          Now on {target.harness}. Running roles restart in a new session with a handoff of their open tasks; their old session stays
          readable.
        </p>
        <label>
          New harness{" "}
          <input required list="switch-harness-options" value={harness} onChange={(event) => setHarness(event.target.value.trim())} />
          <datalist id="switch-harness-options">
            {harnesses.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {`${entry.label ?? entry.id} (${READINESS_LABELS[readiness(entry.id)]})`}
              </option>
            ))}
          </datalist>
        </label>
        {harness ? (
          <p className={chosenReadiness === "ready" || chosenReadiness === "unknown" ? "muted small" : "error-text"}>
            {harness} on this host: {READINESS_LABELS[chosenReadiness]}
          </p>
        ) : null}
        <label>
          Model (optional; blank uses the harness default) <input value={model} onChange={(event) => setModel(event.target.value)} />
        </label>
        {sameHarness.length > 1 ? (
          <fieldset>
            <legend>Which roles</legend>
            <label>
              <input type="radio" name="switch-scope" checked={scope === "role"} onChange={() => setScope("role")} /> Only the{" "}
              {roleTitle(target.role)}
            </label>
            <label>
              <input type="radio" name="switch-scope" checked={scope === "harness"} onChange={() => setScope("harness")} /> Every role on{" "}
              {target.harness}: {sameHarness.map(roleTitle).join(", ")}
            </label>
          </fieldset>
        ) : null}
        {error ? (
          <p className="error-text" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy || !harness}>
            {busy ? "Switching…" : `Switch ${affected.length === 1 ? roleTitle(affected[0]) : `${affected.length} roles`}`}
          </button>
        </div>
      </form>
    </dialog>
  );
}
