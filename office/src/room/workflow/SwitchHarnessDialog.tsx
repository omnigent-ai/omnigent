import { useEffect, useRef, useState } from "react";
import { roleTitle, type AgentRoleId } from "../../../shared/sdlcTheme.ts";
import { READINESS_LABELS, type RoleHealth } from "../../../shared/troubleshoot.ts";
import { officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { EffortSelect, HarnessSelect, ModelSelect, useHarnessReadiness } from "../../RuntimePickers.tsx";

interface SwitchHarnessDialogProps {
  branchId: string;
  hostId: string | undefined;
  target: RoleHealth;
  /** Persistent roles in the branch, to offer moving every role off a failing harness. */
  roles: RoleHealth[];
  reason: string;
  /** Preselect another ready harness, as when the current one is failing. */
  preferOtherHarness?: boolean;
  onClose: () => void;
  onSwitched: (message: string) => void;
}

/** Change a role's harness or model, or move every role on the same harness, restarting running roles with a handoff. */
export function SwitchHarnessDialog({ branchId, hostId, target, roles, reason, preferOtherHarness = false, onClose, onSwitched }: SwitchHarnessDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const harnesses = (useRuntimeOptions()?.harnesses.data ?? []).filter((entry) => entry.id !== target.harness);
  const readiness = useHarnessReadiness(hostId);
  const otherHarness = harnesses.find((entry) => readiness(entry.id) === "ready")?.id ?? harnesses[0]?.id ?? target.harness;
  const [harness, setHarness] = useState(preferOtherHarness ? otherHarness : target.harness);
  const [model, setModel] = useState(preferOtherHarness ? "" : (target.model ?? ""));
  const [effort, setEffort] = useState(preferOtherHarness ? "" : (target.effort ?? ""));
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
  const unchanged = harness === target.harness && model.trim() === (target.model ?? "") && effort === (target.effort ?? "");
  const started = target.state !== "not_started";

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await officeApi.switchHarness(branchId, affected, harness, model.trim(), effort, reason);
      const runtime = `${harness} (${model.trim() || "harness default model"}${effort ? `, ${effort} effort` : ""})`;
      onSwitched(`${affected.map(roleTitle).join(", ")} now on ${runtime}${started ? ", restarted with a handoff" : ""}.`);
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
        <h2 id="switch-harness-title">Change the {roleTitle(target.role)}'s harness, model, or effort</h2>
        <p className="muted small">
          Now on {target.harness} ({target.model ?? "harness default model"}
          {target.effort ? `, ${target.effort} effort` : ""}).{" "}
          {started
            ? "A running role restarts in a new session with a handoff of its open tasks; its old session stays readable."
            : "The role has not started yet; it starts on this choice when the PM first gives it work."}
        </p>
        <label>
          Harness{" "}
          <HarnessSelect
            hostId={hostId}
            value={harness}
            onChange={(next) => {
              setHarness(next);
              setModel(next === target.harness ? (target.model ?? "") : "");
              setEffort(next === target.harness ? (target.effort ?? "") : "");
            }}
          />
        </label>
        {chosenReadiness !== "ready" && chosenReadiness !== "unknown" ? (
          <p className="error-text">
            {harness} on this host: {READINESS_LABELS[chosenReadiness]}
          </p>
        ) : null}
        <label>
          Model <ModelSelect hostId={hostId} harness={harness} value={model} onChange={setModel} />
        </label>
        <label>
          Effort <EffortSelect hostId={hostId} harness={harness} model={model} value={effort} onChange={setEffort} />
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
          <button type="submit" className="primary" disabled={busy || !harness || unchanged}>
            {busy ? "Saving…" : `${started ? "Restart" : "Update"} ${affected.length === 1 ? roleTitle(affected[0]) : `${affected.length} roles`}`}
          </button>
        </div>
      </form>
    </dialog>
  );
}
