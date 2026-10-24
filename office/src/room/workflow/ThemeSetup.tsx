import { useMemo, useState } from "react";
import { AGENT_ROLES, DEFAULT_GATES, GATE_TITLES, ROLES, roleTitle, type AgentRoleId, type GateType } from "../../../shared/sdlcTheme.ts";
import type { BranchRuntime } from "../../../shared/workflowTypes.ts";
import { type HarnessOption, officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";

interface ThemeSetupProps {
  projectId: string;
  onAssigned: () => void;
}

/** What the office can and cannot do on a harness, with the product fallback. */
function capabilityRows(harness: HarnessOption | undefined): { need: string; support: string }[] {
  const capabilities = harness?.capabilities ?? {};
  const value = (key: string) => (capabilities[key] === null || capabilities[key] === undefined ? "unknown" : String(capabilities[key]));
  return [
    { need: "Direct messaging", support: "Supported (session events)" },
    { need: "Resume", support: value("resume") === "none" ? "Not supported: a role rolls over to a new session with a recorded handoff" : value("resume") },
    { need: "Cancellation (Stop)", support: capabilities.interrupt === true ? "Supported" : "Not reported: Stop may only take effect at the next turn" },
    {
      need: "Runtime approvals",
      support: value("elicitation") === "none" ? "No in-turn prompts: gated commands are denied until approved" : value("elicitation"),
    },
    { need: "Event replay", support: "Polled history with stable item ids" },
    { need: "Background execution", support: "Continues while the host and runtime stay up; the browser may close" },
  ];
}

export function ThemeSetup({ projectId, onAssigned }: ThemeSetupProps) {
  const options = useRuntimeOptions();
  const harnesses = options?.harnesses.data ?? [];
  const hosts = (options?.hosts.hosts ?? []).filter((host) => !host.sandbox_provider);
  const [harness, setHarness] = useState("");
  const [model, setModel] = useState("");
  const [hostId, setHostId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [runnerId, setRunnerId] = useState("");
  const [roleModels, setRoleModels] = useState<Partial<Record<AgentRoleId, string>>>({});
  const [gates, setGates] = useState<Record<GateType, boolean>>({ ...DEFAULT_GATES });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedHarness = useMemo(() => harnesses.find((entry) => entry.id === harness), [harnesses, harness]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const runtime: BranchRuntime = { harness };
    if (model.trim()) runtime.model = model.trim();
    if (hostId) runtime.hostId = hostId;
    if (workspace.trim()) runtime.workspace = workspace.trim();
    if (runnerId.trim()) runtime.runnerId = runnerId.trim();
    const overrides = Object.fromEntries(Object.entries(roleModels).filter(([, value]) => value?.trim()));
    if (Object.keys(overrides).length) runtime.roleModels = overrides;
    try {
      await officeApi.assignTheme(projectId, runtime, gates);
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
        <label>
          Harness{" "}
          <input required list="harness-options" value={harness} placeholder="e.g. claude-sdk" onChange={(event) => setHarness(event.target.value.trim())} />
          <datalist id="harness-options">
            {harnesses.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label ?? entry.id}
              </option>
            ))}
          </datalist>
        </label>
        <label>
          Model (optional) <input value={model} onChange={(event) => setModel(event.target.value)} />
        </label>
        <label>
          Host{" "}
          <select value={hostId} onChange={(event) => setHostId(event.target.value)}>
            <option value="">{hosts.length ? "Choose…" : "No hosts connected"}</option>
            {hosts.map((host) => (
              <option key={host.host_id} value={host.host_id}>
                {host.name ?? host.host_id} ({host.status ?? "unknown"})
              </option>
            ))}
          </select>
        </label>
        <label>
          Workspace on the host <input value={workspace} placeholder="/path/to/repo" onChange={(event) => setWorkspace(event.target.value)} />
        </label>
        <details>
          <summary>Advanced</summary>
          <label>
            Runner id (bind to an existing runner instead of a host){" "}
            <input value={runnerId} onChange={(event) => setRunnerId(event.target.value)} />
          </label>
          {AGENT_ROLES.map((role) => (
            <label key={role}>
              {roleTitle(role)} model override{" "}
              <input
                value={roleModels[role] ?? ""}
                onChange={(event) => setRoleModels((current) => ({ ...current, [role]: event.target.value }))}
              />
            </label>
          ))}
        </details>
        <fieldset>
          <legend>Human gates</legend>
          {(Object.keys(GATE_TITLES) as GateType[]).map((gate) => (
            <label key={gate}>
              <input
                type="checkbox"
                checked={gates[gate]}
                onChange={(event) => setGates((current) => ({ ...current, [gate]: event.target.checked }))}
              />{" "}
              {GATE_TITLES[gate]}
            </label>
          ))}
        </fieldset>
        {harness ? (
          <table className="capability-table">
            <caption>Support on {selectedHarness?.label ?? harness}</caption>
            <tbody>
              {capabilityRows(selectedHarness).map((row) => (
                <tr key={row.need}>
                  <th scope="row">{row.need}</th>
                  <td>{row.support}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {error ? (
          <p className="error-text" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="primary" disabled={busy || !harness || (!hostId && !runnerId.trim())}>
          {busy ? "Starting the PM…" : "Assign SDLC"}
        </button>
      </form>
    </section>
  );
}
