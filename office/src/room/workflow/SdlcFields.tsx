import { useMemo } from "react";
import { AGENT_ROLES, DEFAULT_GATES, GATE_TITLES, roleTitle, type AgentRoleId, type GateType } from "../../../shared/sdlcTheme.ts";
import type { BranchRuntime } from "../../../shared/workflowTypes.ts";
import type { HarnessOption } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";

export interface SdlcDraft {
  harness: string;
  model: string;
  runnerId: string;
  roleModels: Partial<Record<AgentRoleId, string>>;
  gates: Record<GateType, boolean>;
}

export function emptySdlcDraft(): SdlcDraft {
  return { harness: "", model: "", runnerId: "", roleModels: {}, gates: { ...DEFAULT_GATES } };
}

/** The runtime fields the draft owns; host and folder are set by the caller. */
export function sdlcRuntime(draft: SdlcDraft): Omit<BranchRuntime, "hostId" | "workspace"> {
  const runtime: Omit<BranchRuntime, "hostId" | "workspace"> = {};
  if (draft.harness.trim()) runtime.harness = draft.harness.trim();
  if (draft.model.trim()) runtime.model = draft.model.trim();
  if (draft.runnerId.trim()) runtime.runnerId = draft.runnerId.trim();
  const overrides = Object.fromEntries(Object.entries(draft.roleModels).filter(([, value]) => value?.trim()));
  if (Object.keys(overrides).length) runtime.roleModels = overrides;
  return runtime;
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

/** Agent type, models, runner, and gates for an SDLC branch. */
export function SdlcFields({ draft, onChange }: { draft: SdlcDraft; onChange: (draft: SdlcDraft) => void }) {
  const options = useRuntimeOptions();
  const harnesses = options?.harnesses.data ?? [];
  const selectedHarness = useMemo(() => harnesses.find((entry) => entry.id === draft.harness), [harnesses, draft.harness]);
  const update = (patch: Partial<SdlcDraft>) => onChange({ ...draft, ...patch });

  return (
    <>
      <label>
        Agent harness (optional, overrides every role){" "}
        <input
          list="harness-options"
          value={draft.harness}
          placeholder="Per-role defaults"
          onChange={(event) => update({ harness: event.target.value.trim() })}
        />
        <datalist id="harness-options">
          {harnesses.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label ?? entry.id}
            </option>
          ))}
        </datalist>
      </label>
      <label>
        Model (optional) <input value={draft.model} onChange={(event) => update({ model: event.target.value })} />
      </label>
      <details>
        <summary>Advanced</summary>
        <label>
          Runner id (bind to an existing runner instead of a host){" "}
          <input value={draft.runnerId} onChange={(event) => update({ runnerId: event.target.value })} />
        </label>
        {AGENT_ROLES.map((role) => (
          <label key={role}>
            {roleTitle(role)} model override{" "}
            <input
              value={draft.roleModels[role] ?? ""}
              onChange={(event) => update({ roleModels: { ...draft.roleModels, [role]: event.target.value } })}
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
              checked={draft.gates[gate]}
              onChange={(event) => update({ gates: { ...draft.gates, [gate]: event.target.checked } })}
            />{" "}
            {GATE_TITLES[gate]}
          </label>
        ))}
      </fieldset>
      {draft.harness ? (
        <table className="capability-table">
          <caption>Support on {selectedHarness?.label ?? draft.harness}</caption>
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
    </>
  );
}
