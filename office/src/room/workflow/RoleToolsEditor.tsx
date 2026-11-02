import { useEffect, useState } from "react";
import { MAX_EXTRA_MCP_SERVERS, RISK_POLICY_LABELS, type RiskPolicy, type RoleToolReport, type RoleToolSettings } from "../../../shared/roleTools.ts";
import { RoleTools } from "./WorkflowSections.tsx";

const RISK_POLICY_NOTES: Record<RiskPolicy, string> = {
  ask: "Outward commands such as git push, merging a PR, or deploying wait for your approval.",
  deny: "Outward commands are refused outright; the role must ask you to run them.",
  catastrophic_only: "Only irreversible commands (force-push, rm -rf /) are blocked. Use only for trusted unattended work.",
};

export interface OfficeToolChoice {
  name: string;
  description: string;
  required: boolean;
}

interface ToolSettingsFieldsProps {
  roleName: string;
  /** The office tools the role may have. */
  tools: OfficeToolChoice[];
  settings: RoleToolSettings;
  onChange: (settings: RoleToolSettings) => void;
  /** The office's gate policies on the role, shown for reference. */
  gatePolicies?: string[];
}

/** A role's office tools, extra MCP servers, and command-safety policy, as plain fields. */
export function ToolSettingsFields({ roleName, tools, settings, onChange, gatePolicies = [] }: ToolSettingsFieldsProps) {
  const disabled = new Set(settings.disabledTools ?? []);
  const servers = settings.mcpServers ?? [];
  const risk = settings.riskPolicy ?? "ask";
  const update = (patch: Partial<RoleToolSettings>) => onChange({ ...settings, ...patch });
  const setServer = (index: number, patch: Partial<{ name: string; url: string }>) =>
    update({ mcpServers: servers.map((server, position) => (position === index ? { ...server, ...patch } : server)) });

  return (
    <>
      <fieldset>
        <legend>Office tools</legend>
        <p className="muted small">Turning a tool off takes effect at once. The {roleName} also has its harness's own tools (shell, file edits, search).</p>
        <ul className="tool-toggles">
          {tools.map((tool) => (
            <li key={tool.name}>
              <label title={tool.description}>
                <input
                  type="checkbox"
                  checked={!disabled.has(tool.name)}
                  disabled={tool.required}
                  onChange={(event) => {
                    const next = new Set(disabled);
                    if (event.target.checked) next.delete(tool.name);
                    else next.add(tool.name);
                    update({ disabledTools: [...next] });
                  }}
                />{" "}
                <code>{tool.name}</code>
                {tool.required ? <span className="muted small"> · required</span> : null}
              </label>
            </li>
          ))}
        </ul>
      </fieldset>

      <fieldset>
        <legend>MCP servers</legend>
        <ul className="mcp-server-rows">
          <li className="muted small">
            <code>office</code> · built in · the office's workflow tools
          </li>
          {servers.map((server, index) => (
            <li key={index} className="mcp-server-row">
              <input aria-label="MCP server name" placeholder="name" value={server.name} onChange={(event) => setServer(index, { name: event.target.value })} />
              <input aria-label="MCP server URL" placeholder="https://…/mcp" value={server.url} onChange={(event) => setServer(index, { url: event.target.value })} />
              <button
                type="button"
                aria-label={`Remove ${server.name || "this server"}`}
                onClick={() => update({ mcpServers: servers.filter((_, position) => position !== index) })}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <button type="button" className="link-button" disabled={servers.length >= MAX_EXTRA_MCP_SERVERS} onClick={() => update({ mcpServers: [...servers, { name: "", url: "" }] })}>
          + Add MCP server
        </button>
      </fieldset>

      <fieldset>
        <legend>Policies</legend>
        <p className="muted small">
          <code>blast_radius</code> (Omnigent) decides how shell commands with a large blast radius are handled.
        </p>
        {(Object.keys(RISK_POLICY_LABELS) as RiskPolicy[]).map((option) => (
          <label key={option} className="risk-option">
            <input type="radio" name={`risk-policy-${roleName}`} checked={risk === option} onChange={() => update({ riskPolicy: option })} /> {RISK_POLICY_LABELS[option]}
            <span className="muted small"> — {RISK_POLICY_NOTES[option]}</span>
          </label>
        ))}
        {gatePolicies.length ? (
          <p className="muted small">Office gates also apply: {gatePolicies.join(", ")}. They follow the room's human gates and the approvals you give.</p>
        ) : null}
      </fieldset>
    </>
  );
}

interface RoleToolsEditorProps {
  report: RoleToolReport;
  roleName: string;
  /** The role has a session that a restart would apply changes to. */
  canRestart: boolean;
  onSave: (settings: RoleToolSettings, restart: boolean) => Promise<void>;
}

function sameSettings(a: RoleToolSettings, b: RoleToolSettings): boolean {
  const normal = (settings: RoleToolSettings) =>
    JSON.stringify({ disabled: [...(settings.disabledTools ?? [])].sort(), servers: settings.mcpServers ?? [], risk: settings.riskPolicy ?? "ask" });
  return normal(a) === normal(b);
}

/** A live role's tools and policies: the fields, Save, and what its session has now. */
export function RoleToolsEditor({ report, roleName, canRestart, onSave }: RoleToolsEditorProps) {
  const [draft, setDraft] = useState<RoleToolSettings>(report.settings);
  const [busy, setBusy] = useState(false);

  // Start again from the saved settings when the role changes or they reload.
  useEffect(() => setDraft(report.settings), [report]);

  const dirty = !sameSettings(draft, report.settings);
  const save = async (restart: boolean) => {
    setBusy(true);
    try {
      await onSave(draft, restart);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="role-tools-editor">
      {report.restartPending ? (
        <p className="restart-pending small" role="status">
          Saved MCP server or policy changes reach the {roleName} when it restarts.
        </p>
      ) : null}
      <ToolSettingsFields
        roleName={roleName}
        tools={report.allOfficeTools}
        settings={draft}
        onChange={setDraft}
        gatePolicies={report.policies.filter((policy) => policy.name.startsWith("office_")).map((policy) => policy.name)}
      />
      <div className="team-config-actions">
        <button type="button" className="primary" disabled={!dirty || busy} onClick={() => void save(false)}>
          Save
        </button>
        {canRestart ? (
          <button type="button" disabled={!dirty || busy} onClick={() => void save(true)}>
            Save and restart
          </button>
        ) : null}
        {dirty ? (
          <button type="button" disabled={busy} onClick={() => setDraft(report.settings)}>
            Discard
          </button>
        ) : null}
      </div>
      <details className="role-tools-live">
        <summary>What the session has now</summary>
        <RoleTools report={report} />
      </details>
    </div>
  );
}
