import { useEffect, useState } from "react";
import { type RoleToolSettings, requiredTools } from "../../../shared/roleTools.ts";
import { COORDINATOR_ROLE, DEFAULT_GATES, GATE_TITLES, roleAgentName, type GateType } from "../../../shared/sdlcTheme.ts";
import { type TeamDefinition, hasRole, shortRoleTitle, sameTeam, sdlcTeam, teamProblems, teamRole } from "../../../shared/team.ts";
import { DEFAULT_TEAM_TEMPLATE, type TeamTemplate, templateTeam } from "../../../shared/teamTemplates.ts";
import type { BranchRuntime } from "../../../shared/workflowTypes.ts";
import { type HarnessOption, officeApi } from "../../api.ts";
import { teamFileName, teamFileYaml } from "../../../shared/teamFile.ts";
import { downloadText, pickTextFile } from "../../teamFileTransfer.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { PersonSprite } from "../../PersonSprite.tsx";
import { EffortSelect, HarnessSelect, ModelSelect } from "../../RuntimePickers.tsx";
import { workerPalette } from "../../sprites.ts";
import { type OfficeToolChoice, ToolSettingsFields } from "./RoleToolsEditor.tsx";
import { RaciMatrixEditor, RoleDefinitionEditor, RoleRaciEditor, TeamActions, TeamNav } from "./TeamEditing.tsx";

type RoleValues = Record<string, string>;

/** Everything about a new SDLC room's team, before the room exists. */
export interface SdlcDraft {
  runnerId: string;
  gates: Record<GateType, boolean>;
  team: TeamDefinition;
  roleHarnesses: RoleValues;
  roleModels: RoleValues;
  roleEfforts: RoleValues;
  roleCustomInstructions: RoleValues;
  roleToolSettings: Record<string, RoleToolSettings>;
  /** The template the team still matches; cleared by any edit. */
  teamTemplateId: string | null;
  /** Where the team came from: a template's id, or "custom" to build it role by role. */
  teamSource: string;
}

export const CUSTOM_TEAM = "custom";

export function emptySdlcDraft(): SdlcDraft {
  return {
    runnerId: "",
    gates: { ...DEFAULT_GATES },
    team: sdlcTeam(),
    roleHarnesses: {},
    roleModels: {},
    roleEfforts: {},
    roleCustomInstructions: {},
    roleToolSettings: {},
    teamTemplateId: DEFAULT_TEAM_TEMPLATE.id,
    teamSource: DEFAULT_TEAM_TEMPLATE.id,
  };
}

/** The draft with a template's team and role setups; the runner and gates stay. */
export function draftFromTemplate(draft: SdlcDraft, template: TeamTemplate): SdlcDraft {
  const pick = (read: (entry: NonNullable<TeamTemplate["roles"][string]>) => string | undefined) =>
    Object.fromEntries(Object.entries(template.roles).flatMap(([role, entry]) => (entry && read(entry) ? [[role, read(entry) as string]] : [])));
  return {
    ...draft,
    team: templateTeam(template),
    roleHarnesses: pick((entry) => entry.harness),
    roleModels: pick((entry) => entry.model),
    roleEfforts: pick((entry) => entry.effort),
    roleCustomInstructions: pick((entry) => entry.instructions),
    roleToolSettings: Object.fromEntries(Object.entries(template.roles).flatMap(([role, entry]) => (entry?.toolSettings ? [[role, entry.toolSettings]] : []))),
    teamTemplateId: template.id,
    teamSource: template.id,
  };
}

/** The runtime fields the draft owns; host and folder are set by the caller. */
export function sdlcRuntime(draft: SdlcDraft): Omit<BranchRuntime, "hostId" | "workspace"> {
  const onTeam = (values: RoleValues) =>
    Object.fromEntries(Object.entries(values).filter(([role, value]) => hasRole(draft.team, role) && value?.trim()).map(([role, value]) => [role, value.trim()]));
  const runtime: Omit<BranchRuntime, "hostId" | "workspace"> = {};
  if (draft.runnerId.trim()) runtime.runnerId = draft.runnerId.trim();
  if (!sameTeam(draft.team, sdlcTeam())) runtime.team = draft.team;
  const harnesses = onTeam(draft.roleHarnesses);
  if (Object.keys(harnesses).length) runtime.roleHarnesses = harnesses;
  const models = onTeam(draft.roleModels);
  if (Object.keys(models).length) runtime.roleModels = models;
  const efforts = onTeam(draft.roleEfforts);
  if (Object.keys(efforts).length) runtime.roleEfforts = efforts;
  const instructions = onTeam(draft.roleCustomInstructions);
  if (Object.keys(instructions).length) runtime.roleCustomInstructions = instructions;
  const tools = Object.fromEntries(Object.entries(draft.roleToolSettings).filter(([role]) => hasRole(draft.team, role)));
  if (Object.keys(tools).length) runtime.roleToolSettings = tools;
  if (draft.teamTemplateId) runtime.teamTemplateId = draft.teamTemplateId;
  return runtime;
}

/** Why the draft cannot start a room yet, if anything. */
export function sdlcDraftProblems(draft: SdlcDraft): string[] {
  return teamProblems(draft.team);
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

interface OfficeTool {
  name: string;
  description: string;
  coordinatorOnly: boolean;
}

function toolChoices(tools: OfficeTool[], role: string): OfficeToolChoice[] {
  const required = requiredTools(role);
  return tools
    .filter((tool) => !tool.coordinatorOnly || role === COORDINATOR_ROLE)
    .map((tool) => ({ name: tool.name, description: tool.description, required: required.includes(tool.name) }));
}

/** A new room's team, set up in the Team configuration layout: the team, then each role. */
export function SdlcTeamEditor({
  draft,
  hostId,
  onChange,
  onTemplatesChanged,
}: {
  draft: SdlcDraft;
  hostId: string | undefined;
  onChange: (draft: SdlcDraft) => void;
  /** An imported team file was saved as a template. */
  onTemplatesChanged?: () => void;
}) {
  const [selected, setSelected] = useState("team");
  const [templates, setTemplates] = useState<TeamTemplate[]>([]);
  const [officeTools, setOfficeTools] = useState<OfficeTool[]>([]);
  useEffect(() => {
    officeApi.teamTemplates().then(setTemplates).catch(() => undefined);
    officeApi.officeTools().then(setOfficeTools).catch(() => undefined);
  }, []);

  // Any edit means the team no longer matches its template exactly.
  const update = (patch: Partial<SdlcDraft>) => onChange({ ...draft, ...patch, teamTemplateId: null });
  const setTeam = (team: TeamDefinition) => update({ team });
  const role = selected === "team" ? undefined : teamRole(draft.team, selected);
  const setRoleValue = (field: "roleHarnesses" | "roleModels" | "roleEfforts" | "roleCustomInstructions", id: string, value: string) =>
    update({ [field]: { ...draft[field], [id]: value } });
  const template = templates.find((entry) => entry.id === draft.teamSource);
  const custom = draft.teamSource === CUSTOM_TEAM;
  const [fileNotice, setFileNotice] = useState<{ text: string; error: boolean } | null>(null);

  const exportYaml = () => {
    const name = template?.name ?? "New room team";
    downloadText(teamFileName(name), teamFileYaml(sdlcRuntime(draft) as BranchRuntime, name, template?.description ?? null));
  };

  // The file is saved as a template, then the draft starts from it like any other.
  const importYaml = async () => {
    setFileNotice(null);
    try {
      const text = await pickTextFile();
      if (text === null) return;
      const { template: imported, replaced } = await officeApi.importTeamTemplate(text);
      setTemplates(await officeApi.teamTemplates());
      onTemplatesChanged?.();
      onChange(draftFromTemplate(draft, imported));
      setSelected("team");
      setFileNotice({ text: `${replaced ? "Replaced" : "Imported"} the template "${imported.name}" and started the team from it.`, error: false });
    } catch (caught) {
      setFileNotice({ text: (caught as Error).message, error: true });
    }
  };

  return (
    // Enter in a field here edits the team; it must not submit the surrounding New room form.
    <div
      className="team-setup"
      onKeyDown={(event) => {
        if (event.key === "Enter" && (event.target as HTMLElement).tagName === "INPUT") event.preventDefault();
      }}
    >
      <div className="team-templates-bar">
        <label className="inline-field">
          Start from template{" "}
          <select
            value={custom ? CUSTOM_TEAM : draft.teamSource}
            onChange={(event) => {
              if (event.target.value === CUSTOM_TEAM) {
                // Custom keeps the team as it is and lets roles be added one by one.
                onChange({ ...draft, teamSource: CUSTOM_TEAM, teamTemplateId: null });
                return;
              }
              const chosen = templates.find((entry) => entry.id === event.target.value);
              if (chosen) {
                onChange(draftFromTemplate(draft, chosen));
                setSelected("team");
              }
            }}
          >
            <option value={CUSTOM_TEAM}>Custom</option>
            {templates.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
                {entry.builtIn ? " (built in)" : ""}
                {entry.id === draft.teamSource && !draft.teamTemplateId ? " (modified)" : ""}
              </option>
            ))}
          </select>
        </label>
        <span className="muted small">
          {draft.team.roles.length} role{draft.team.roles.length === 1 ? "" : "s"} · {draft.team.taskTypes.length} task types{custom ? " · custom: add roles on the left" : template ? ` · from "${template.name}"${draft.teamTemplateId ? "" : ", modified"}; remove roles with ×` : ""}
        </span>
        <button type="button" onClick={exportYaml}>
          Export YAML
        </button>
        <button type="button" onClick={() => void importYaml()}>
          Import YAML…
        </button>
      </div>
      {fileNotice ? (
        <p className={`small ${fileNotice.error ? "error-text" : "muted"}`} role={fileNotice.error ? "alert" : "status"}>
          {fileNotice.text}
        </p>
      ) : null}
      <div className="team-config-body">
        <TeamNav
          roles={draft.team.roles.map((entry) => ({ id: entry.id, title: entry.title, marked: Boolean(draft.roleCustomInstructions[entry.id]?.trim()) }))}
          selected={role ? role.id : "team"}
          onSelect={setSelected}
          team={draft.team}
          onTeamChange={setTeam}
          allowAdd={custom}
          allowRemove
        />
        {!role ? (
          <section className="team-config-detail" aria-label="Whole team setup">
            <div className="team-config-title">
              <h3>Whole team</h3>
            </div>
            <h4>
              RACI and task types <span className="muted small">· the PM creates tasks of these types; R and A decide who does each</span>
            </h4>
            <RaciMatrixEditor team={draft.team} onChange={setTeam} />
            <TeamActions saved={draft.team} draft={draft.team} onDraftChange={setTeam} busy={false} />
          </section>
        ) : (
          <section className="team-config-detail" aria-label={`${role.title} setup`}>
            <div className="team-config-title">
              <PersonSprite palette={workerPalette(roleAgentName(role.id))} />
              <h3>{role.title}</h3>
              <span className="muted small">{role.id === COORDINATOR_ROLE ? "Starts with the room and coordinates it" : `Starts when the ${shortRoleTitle(draft.team.roles[0]?.title ?? "PM")} first gives it work`}</span>
            </div>

            <h4>Role</h4>
            <RoleDefinitionEditor team={draft.team} role={role} hostId={hostId} onChange={setTeam} onRemoved={() => setSelected("team")} />

            <h4>Runtime</h4>
            <div className="role-runtime-fields">
              <label>
                Harness
                <HarnessSelect
                  hostId={hostId}
                  value={draft.roleHarnesses[role.id] ?? ""}
                  emptyLabel={`${role.defaultHarness} (default)`}
                  onChange={(harness) => update({ roleHarnesses: { ...draft.roleHarnesses, [role.id]: harness }, roleModels: { ...draft.roleModels, [role.id]: "" }, roleEfforts: { ...draft.roleEfforts, [role.id]: "" } })}
                />
              </label>
              <label>
                Model
                <ModelSelect
                  hostId={hostId}
                  harness={draft.roleHarnesses[role.id] || role.defaultHarness}
                  value={draft.roleModels[role.id] ?? ""}
                  onChange={(model) => setRoleValue("roleModels", role.id, model)}
                />
              </label>
              <label>
                Effort
                <EffortSelect
                  hostId={hostId}
                  harness={draft.roleHarnesses[role.id] || role.defaultHarness}
                  model={draft.roleModels[role.id] ?? ""}
                  value={draft.roleEfforts[role.id] ?? ""}
                  onChange={(effort) => {
                    if ((draft.roleEfforts[role.id] ?? "") !== effort) setRoleValue("roleEfforts", role.id, effort);
                  }}
                />
              </label>
            </div>

            <h4>Tasks and RACI</h4>
            <RoleRaciEditor team={draft.team} role={role.id} onChange={setTeam} />
            <TeamActions saved={draft.team} draft={draft.team} onDraftChange={setTeam} busy={false} />

            <h4>Tools and policies</h4>
            <div className="role-tools-editor">
              <ToolSettingsFields
                roleName={role.title}
                tools={toolChoices(officeTools, role.id)}
                settings={draft.roleToolSettings[role.id] ?? {}}
                onChange={(settings) => update({ roleToolSettings: { ...draft.roleToolSettings, [role.id]: settings } })}
              />
            </div>

            <h4>Role prompt</h4>
            <label className="team-config-instructions">
              <span>
                Your instructions for the {role.title} <span className="muted small">(added to the end of its prompt, after the brief above)</span>
              </span>
              <textarea
                rows={4}
                maxLength={8000}
                value={draft.roleCustomInstructions[role.id] ?? ""}
                placeholder={`For example: "Always add a regression test for each fix."`}
                onChange={(event) => setRoleValue("roleCustomInstructions", role.id, event.target.value)}
              />
            </label>
          </section>
        )}
      </div>
    </div>
  );
}

/** Human gates and the advanced runner choice for a new SDLC room. */
export function SdlcFields({ draft, onChange }: { draft: SdlcDraft; onChange: (draft: SdlcDraft) => void }) {
  const update = (patch: Partial<SdlcDraft>) => onChange({ ...draft, ...patch });
  const harnesses = useRuntimeOptions()?.harnesses.data ?? [];
  const used = [...new Set(draft.team.roles.map((role) => draft.roleHarnesses[role.id] || role.defaultHarness))];
  return (
    <>
      <fieldset>
        <legend>Human gates</legend>
        {(Object.keys(GATE_TITLES) as GateType[]).map((gate) => (
          <label key={gate}>
            <input type="checkbox" checked={draft.gates[gate]} onChange={(event) => update({ gates: { ...draft.gates, [gate]: event.target.checked } })} />{" "}
            {GATE_TITLES[gate]}
          </label>
        ))}
      </fieldset>
      <details>
        <summary>Advanced: runner and harness support</summary>
        <label>
          Runner id (bind to an existing runner instead of a host){" "}
          <input value={draft.runnerId} onChange={(event) => update({ runnerId: event.target.value })} />
        </label>
        {used.map((harness) => {
          const option = harnesses.find((entry) => entry.id === harness);
          return (
            <table key={harness} className="capability-table">
              <caption>Support on {option?.label ?? harness}</caption>
              <tbody>
                {capabilityRows(option).map((row) => (
                  <tr key={row.need}>
                    <th scope="row">{row.need}</th>
                    <td>{row.support}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          );
        })}
      </details>
    </>
  );
}
