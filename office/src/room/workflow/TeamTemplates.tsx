import { useCallback, useEffect, useState } from "react";
import { TEMPLATE_FIELD_LABELS, type TeamTemplate, templateDiff } from "../../../shared/teamTemplates.ts";
import type { BranchView } from "../../../shared/workflowTypes.ts";
import { officeApi } from "../../api.ts";
import { teamFileName, teamFileYaml } from "../../../shared/teamFile.ts";
import { downloadText, pickTextFile } from "../../teamFileTransfer.ts";

interface TeamTemplatesProps {
  view: BranchView;
  /** Names an exported team that is not a saved template. */
  roomName: string;
  /** Roles that have a session, so applying can offer to restart them. */
  startedRoles: Set<string>;
  onApplied: (message: string) => void;
}

/** Save the room's team as a template, or switch the room to another template. */
export function TeamTemplates({ view, roomName, startedRoles, onApplied }: TeamTemplatesProps) {
  const [templates, setTemplates] = useState<TeamTemplate[]>([]);
  const [panel, setPanel] = useState<"save" | "switch" | null>(null);
  const [chosenId, setChosenId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runtime = view.branch.runtime;
  const branchId = view.branch.id;

  const load = useCallback(() => officeApi.teamTemplates().then(setTemplates).catch(() => undefined), []);
  useEffect(() => {
    void load();
  }, [load]);

  const current = templates.find((template) => template.id === runtime.teamTemplateId) ?? null;
  const modified = current ? (() => {
    const diff = templateDiff(runtime, current);
    return diff.roles.length + diff.taskTypes.length > 0;
  })() : false;
  const chosen = templates.find((template) => template.id === chosenId) ?? null;
  const diff = chosen ? templateDiff(runtime, chosen) : { roles: [], taskTypes: [] };
  const changes = diff.roles;
  const anyChange = changes.length + diff.taskTypes.length > 0;
  // Roles joining start fresh and roles leaving stop, so only staying roles restart.
  const restartable = changes.filter(
    (change) => !change.fields.includes("added") && !change.fields.includes("removed") && (startedRoles.has(change.role) || change.role === "pm"),
  );
  const replacing = templates.find((template) => !template.builtIn && template.name.toLowerCase() === name.trim().toLowerCase()) ?? null;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const openSave = () => {
    setName(current && !current.builtIn ? current.name : "");
    setDescription(current && !current.builtIn ? (current.description ?? "") : "");
    setError(null);
    setPanel(panel === "save" ? null : "save");
  };

  const exportYaml = () => {
    const name = current?.name ?? `${roomName} team`;
    downloadText(teamFileName(name), teamFileYaml(runtime, name, current?.description ?? null));
  };

  // An imported file becomes a saved template, shown in Switch with what it would change.
  const importYaml = () =>
    void run(async () => {
      const text = await pickTextFile();
      if (text === null) return;
      const { template, replaced } = await officeApi.importTeamTemplate(text);
      await load();
      setChosenId(template.id);
      setPanel("switch");
      onApplied(`${replaced ? "Replaced" : "Imported"} the template "${template.name}". Review it below, then apply it.`);
    });

  const openSwitch = () => {
    setChosenId(null);
    setError(null);
    setPanel(panel === "switch" ? null : "switch");
  };

  return (
    <section className="team-templates" aria-label="Team template">
      <div className="team-templates-bar">
        <span>
          Template: <strong>{current ? current.name : "None"}</strong>
          {modified ? <span className="muted small"> · modified since</span> : null}
        </span>
        <button type="button" aria-expanded={panel === "save"} onClick={openSave}>
          Save as template…
        </button>
        <button type="button" aria-expanded={panel === "switch"} onClick={openSwitch}>
          Switch template…
        </button>
        <button type="button" onClick={exportYaml} title="Download this room's team, roles, RACI, and role setups as a YAML file">
          Export YAML
        </button>
        <button type="button" disabled={busy} onClick={importYaml} title="Import a team YAML file as a template">
          Import YAML…
        </button>
      </div>

      {panel === "save" ? (
        <form
          className="team-templates-panel"
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              const { template } = await officeApi.saveTeamTemplate(branchId, name, description);
              await load();
              setPanel(null);
              onApplied(`Saved the team as "${template.name}".`);
            });
          }}
        >
          <p className="muted small">
            Saves each role's harness, model, effort, instructions, tools, and policies. The room's folder and human gates are not part of a template.
          </p>
          <label>
            Name <input required maxLength={60} value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Claude-heavy team" />
          </label>
          <label>
            Description (optional) <input maxLength={300} value={description} onChange={(event) => setDescription(event.target.value)} />
          </label>
          {replacing ? <p className="small warn-text">This replaces the saved template "{replacing.name}".</p> : null}
          <div className="team-config-actions">
            <button type="submit" className="primary" disabled={busy || !name.trim()}>
              {replacing ? "Replace template" : "Save template"}
            </button>
            <button type="button" disabled={busy} onClick={() => setPanel(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}

      {panel === "switch" ? (
        <div className="team-templates-panel">
          <ul className="template-list" role="radiogroup" aria-label="Team templates">
            {templates.map((template) => (
              <li key={template.id}>
                <label className={template.id === chosenId ? "template-chosen" : undefined}>
                  <input type="radio" name="team-template" checked={template.id === chosenId} onChange={() => setChosenId(template.id)} />
                  <span>
                    <strong>{template.name}</strong>
                    {template.builtIn ? <span className="muted small"> · built in</span> : null}
                    {template.id === runtime.teamTemplateId ? <span className="muted small"> · current</span> : null}
                    <span className="muted small template-description">
                      {template.description ?? `${Object.keys(template.roles).length} role(s) customized`}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {chosen ? (
            <div className="template-preview">
              {!anyChange ? (
                <p className="muted small">This room's team already matches "{chosen.name}".</p>
              ) : (
                <>
                  <p className="small">Switching to "{chosen.name}" changes:</p>
                  <ul className="small">
                    {changes.map((change) => (
                      <li key={change.role}>
                        <strong>{change.title}</strong>: {change.fields.map((field) => TEMPLATE_FIELD_LABELS[field]).join(", ")}
                        {startedRoles.has(change.role) ? <span className="muted"> · running</span> : null}
                      </li>
                    ))}
                    {diff.taskTypes.length ? (
                      <li>
                        <strong>Task types</strong>: {diff.taskTypes.join(", ")}
                      </li>
                    ) : null}
                  </ul>
                </>
              )}
              <div className="team-config-actions">
                <button
                  type="button"
                  className="primary"
                  disabled={busy || !anyChange}
                  onClick={() =>
                    void run(async () => {
                      await officeApi.applyTeamTemplate(branchId, chosen.id, false);
                      setPanel(null);
                      onApplied(`Switched to "${chosen.name}". Running roles pick it up when they restart.`);
                    })
                  }
                >
                  Apply
                </button>
                {restartable.length ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await officeApi.applyTeamTemplate(branchId, chosen.id, true);
                        setPanel(null);
                        onApplied(`Switched to "${chosen.name}" and restarted ${restartable.map((change) => change.title).join(", ")}.`);
                      })
                    }
                  >
                    Apply and restart {restartable.length} changed role{restartable.length === 1 ? "" : "s"}
                  </button>
                ) : null}
                {!chosen.builtIn ? (
                  <button
                    type="button"
                    className="danger"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await officeApi.deleteTeamTemplate(chosen.id);
                        setChosenId(null);
                        await load();
                      })
                    }
                  >
                    Delete template
                  </button>
                ) : null}
                <button type="button" disabled={busy} onClick={() => setPanel(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <p className="muted small">Choose a template to see what would change.</p>
          )}
        </div>
      ) : null}
      {error ? (
        <p className="error-text small" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
