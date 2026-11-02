import { useEffect, useRef, useState } from "react";
import type { BuildingSnapshot } from "../../shared/types.ts";
import { officeApi } from "../api.ts";
import { HostFolderFields } from "../HostFolderFields.tsx";
import { NATIVE_HARNESSES, NATIVE_AGENT_TITLE, shortRoleTitle } from "../../shared/team.ts";
import { DEFAULT_TEAM_TEMPLATE, NATIVE_TEAM_TEMPLATE, type TeamTemplate } from "../../shared/teamTemplates.ts";
import { HarnessSelect } from "../RuntimePickers.tsx";
import { CUSTOM_TEAM, SdlcFields, SdlcTeamEditor, draftFromTemplate, emptySdlcDraft, sdlcDraftProblems, sdlcRuntime } from "../room/workflow/SdlcFields.tsx";

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
  const [templates, setTemplates] = useState<TeamTemplate[]>([]);
  const loadTemplates = () => officeApi.teamTemplates().then(setTemplates).catch(() => undefined);
  useEffect(() => {
    void loadTemplates();
  }, []);
  // The Theme choice also picks the team: the SDLC default, the native single agent, a custom team, or a saved template.
  const themeValue =
    theme === "none"
      ? "none"
      : draft.teamSource === DEFAULT_TEAM_TEMPLATE.id
        ? "sdlc"
        : draft.teamSource === NATIVE_TEAM_TEMPLATE.id
          ? "native"
          : draft.teamSource === CUSTOM_TEAM
            ? CUSTOM_TEAM
            : `template:${draft.teamSource}`;
  const native = theme === "sdlc" && draft.teamSource === NATIVE_TEAM_TEMPLATE.id;
  const chooseTheme = (value: string) => {
    if (value === "none") {
      setTheme("none");
      return;
    }
    setTheme("sdlc");
    if (value === CUSTOM_TEAM) {
      setDraft((current) => ({ ...current, teamSource: CUSTOM_TEAM, teamTemplateId: null }));
      return;
    }
    const id = value === "sdlc" ? DEFAULT_TEAM_TEMPLATE.id : value === "native" ? NATIVE_TEAM_TEMPLATE.id : value.slice("template:".length);
    const template = templates.find((entry) => entry.id === id) ?? [DEFAULT_TEAM_TEMPLATE, NATIVE_TEAM_TEMPLATE].find((entry) => entry.id === id);
    if (template) setDraft((current) => draftFromTemplate(current, template));
  };
  // An SDLC room's team is set up on its own tab, in the Team configuration layout.
  const [tab, setTab] = useState<"room" | "team">("room");
  const sdlc = theme === "sdlc";
  const teamProblems = sdlc ? sdlcDraftProblems(draft) : [];
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const hostFolderReady = !hostId || Boolean(workspace.trim());
  const sdlcReady =
    theme === "none" ? hostFolderReady : ((Boolean(hostId) && Boolean(workspace.trim())) || Boolean(draft.runnerId.trim())) && teamProblems.length === 0;

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
      className={`approval-dialog new-room${sdlc ? " new-room-sdlc" : ""}`}
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
        {sdlc ? (
          <div role="tablist" aria-label="New room" className="workflow-tabs new-room-tabs">
            {(["room", "team"] as const).map((entry) => (
              <button
                key={entry}
                type="button"
                role="tab"
                id={`new-room-tab-${entry}`}
                aria-selected={tab === entry}
                aria-controls="new-room-panel"
                className="workflow-tab"
                onClick={() => setTab(entry)}
              >
                {entry === "room" ? "Room" : `Team · ${draft.team.roles.length} role${draft.team.roles.length === 1 ? "" : "s"}`}
                {entry === "team" && teamProblems.length ? <span className="tab-badge tab-badge-urgent">{teamProblems.length}</span> : null}
              </button>
            ))}
          </div>
        ) : null}
        <div id="new-room-panel" role={sdlc ? "tabpanel" : undefined} aria-labelledby={sdlc ? `new-room-tab-${tab}` : undefined}>
          <div hidden={sdlc && tab !== "room"}>
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
              <select value={themeValue} onChange={(event) => chooseTheme(event.target.value)}>
                <option value="none">None (generalist room)</option>
                <option value="sdlc">SDLC (default team)</option>
                <option value="native">Native (single agent)</option>
                <option value={CUSTOM_TEAM}>Custom team (SDLC with your own roles)</option>
                {templates
                  .filter((entry) => !entry.builtIn)
                  .map((entry) => (
                    <option key={entry.id} value={`template:${entry.id}`}>
                      Template: {entry.name}
                    </option>
                  ))}
              </select>
            </label>
            {native ? (
              <label>
                Native harness{" "}
                <HarnessSelect
                  hostId={hostId || undefined}
                  value={draft.roleHarnesses.pm || NATIVE_TEAM_TEMPLATE.team?.roles[0]?.defaultHarness || ""}
                  only={NATIVE_HARNESSES}
                  onChange={(harness) =>
                    setDraft((current) => ({ ...current, roleHarnesses: { ...current.roleHarnesses, pm: harness }, roleModels: { ...current.roleModels, pm: "" }, roleEfforts: { ...current.roleEfforts, pm: "" } }))
                  }
                />
                <span className="muted small">The {NATIVE_AGENT_TITLE} runs the room alone in this CLI: it plans the tasks and does each one itself.</span>
              </label>
            ) : null}
            {sdlc ? (
              <>
                <SdlcFields draft={draft} onChange={setDraft} />
                <p className="muted small">
                  The team: {draft.team.roles.map((role) => role.title).join(", ")}.{" "}
                  <button type="button" className="link-button" onClick={() => setTab("team")}>
                    Set up roles, RACI, and task types
                  </button>
                </p>
              </>
            ) : null}
          </div>
          {sdlc && tab === "team" ? <SdlcTeamEditor draft={draft} hostId={hostId || undefined} onChange={setDraft} onTemplatesChanged={() => void loadTemplates()} /> : null}
        </div>
        {teamProblems.length && tab === "room" ? (
          <p className="error-text small">The team has {teamProblems.length} problem(s) to fix on the Team tab before the room can start.</p>
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
          <button type="submit" className="primary" disabled={busy || !name.trim() || !sdlcReady}>
            {busy && theme === "sdlc" ? `Starting the ${shortRoleTitle(draft.team.roles[0]?.title ?? "PM")}…` : "Create room"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
