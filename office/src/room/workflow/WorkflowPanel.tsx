import { useState } from "react";
import { AGENT_ROLES, GATE_TITLES, matrixRow, roleTitle } from "../../../shared/sdlcTheme.ts";
import type { BranchView, Requirement } from "../../../shared/workflowTypes.ts";
import { OfficeApiError, officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { DictationButton } from "../../Dictation.tsx";

interface WorkflowPanelProps {
  view: BranchView;
  onChanged: () => void;
}

function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function appendText(current: string, addition: string): string {
  return current.trim() ? `${current.trimEnd()} ${addition}` : addition;
}

function BoardEditor({ view, onChanged }: WorkflowPanelProps) {
  const [draft, setDraft] = useState<Requirement[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [baseRevision, setBaseRevision] = useState(view.board.revision);
  const dictationAvailable = useRuntimeOptions()?.dictationAvailable ?? false;
  const editing = draft !== null;
  const requirements = draft ?? view.board.requirements;

  const update = (index: number, patch: Partial<Requirement>) =>
    setDraft((current) => (current ?? []).map((entry, position) => (position === index ? { ...entry, ...patch } : entry)));

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      await officeApi.updateBoard(view.branch.id, baseRevision, draft.filter((entry) => entry.title.trim()));
      setDraft(null);
      onChanged();
    } catch (caught) {
      setError(
        caught instanceof OfficeApiError && caught.code === "board_conflict"
          ? `${caught.message} Your edits are kept below; discard them to load the latest board.`
          : (caught as Error).message,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Requirements board" className="workflow-section">
      <h3>
        Requirements board <span className="muted small">revision {view.board.revision}</span>
      </h3>
      {requirements.length === 0 && !editing ? <p className="muted">No requirements yet. Add them or start a PM interview.</p> : null}
      <ol className="requirements-list">
        {requirements.map((requirement, index) =>
          editing ? (
            <li key={requirement.id}>
              <label>
                Title <input value={requirement.title} onChange={(event) => update(index, { title: event.target.value })} />
              </label>
              <label>
                Description
                <textarea rows={2} value={requirement.description} onChange={(event) => update(index, { description: event.target.value })} />
              </label>
              <DictationButton available={dictationAvailable} onText={(text) => update(index, { description: appendText(requirement.description, text) })} />
              <label>
                Acceptance criteria (one per line)
                <textarea
                  rows={3}
                  value={requirement.acceptanceCriteria.join("\n")}
                  onChange={(event) => update(index, { acceptanceCriteria: event.target.value.split("\n") })}
                />
              </label>
              <button type="button" onClick={() => setDraft((current) => (current ?? []).filter((_, position) => position !== index))}>
                Remove requirement
              </button>
            </li>
          ) : (
            <li key={requirement.id}>
              <strong>{requirement.title}</strong>
              {requirement.description ? <p>{requirement.description}</p> : null}
              {requirement.acceptanceCriteria.length ? (
                <ul>
                  {requirement.acceptanceCriteria.map((criterion) => (
                    <li key={criterion}>{criterion}</li>
                  ))}
                </ul>
              ) : null}
            </li>
          ),
        )}
      </ol>
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
      {editing ? (
        <div className="composer-actions">
          <button
            type="button"
            onClick={() => setDraft((current) => [...(current ?? []), { id: `req_new_${Date.now()}`, title: "", description: "", acceptanceCriteria: [] }])}
          >
            Add requirement
          </button>
          <button type="button" onClick={() => setDraft(null)} disabled={busy}>
            Discard edits
          </button>
          <button type="button" className="primary" onClick={() => void save()} disabled={busy}>
            Save board
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => {
            setBaseRevision(view.board.revision);
            setError(null);
            setDraft(view.board.requirements.map((entry) => ({ ...entry, acceptanceCriteria: [...entry.acceptanceCriteria] })));
          }}
        >
          Edit requirements
        </button>
      )}
    </section>
  );
}

function Interview({ view, onChanged }: WorkflowPanelProps) {
  const [topic, setTopic] = useState("");
  const [answer, setAnswer] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dictationAvailable = useRuntimeOptions()?.dictationAvailable ?? false;
  const open = view.interviews.find((question) => question.answer === null) ?? null;

  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      setNotice(success);
      onChanged();
    } catch (caught) {
      setNotice((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="PM interview" className="workflow-section">
      <h3>PM interview</h3>
      {open ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              await officeApi.answerInterview(view.branch.id, open.id, answer.trim());
              setAnswer("");
            }, "Answer sent to the PM.");
          }}
        >
          <p className="approval-message">
            <strong>PM asks:</strong> {open.question}
          </p>
          <label htmlFor="interview-answer">Your answer (edit any dictated text before sending)</label>
          <textarea id="interview-answer" rows={3} value={answer} onChange={(event) => setAnswer(event.target.value)} />
          <div className="composer-actions">
            <DictationButton available={dictationAvailable} onText={(text) => setAnswer((current) => appendText(current, text))} />
            <button type="submit" className="primary" disabled={busy || !answer.trim()}>
              Send answer
            </button>
          </div>
        </form>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void run(() => officeApi.startInterview(view.branch.id, topic), "The PM will ask its first question shortly.");
          }}
        >
          <label htmlFor="interview-topic">Interview topic (optional)</label>
          <input id="interview-topic" value={topic} onChange={(event) => setTopic(event.target.value)} />
          <button type="submit" disabled={busy}>
            Start PM interview
          </button>
        </form>
      )}
      {view.interviews.some((question) => question.answer !== null) ? (
        <details>
          <summary>Previous questions</summary>
          <ol>
            {view.interviews
              .filter((question) => question.answer !== null)
              .map((question) => (
                <li key={question.id}>
                  <strong>{question.question}</strong>
                  <p>{question.answer}</p>
                </li>
              ))}
          </ol>
        </details>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}

function Tasks({ view, onChanged }: WorkflowPanelProps) {
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <section aria-label="Tasks" className="workflow-section">
      <h3>Tasks</h3>
      {view.tasks.length === 0 ? <p className="muted">The PM has not created tasks yet.</p> : null}
      <ul className="task-list">
        {view.tasks.map((task) => (
          <li key={task.id} className={`task task-${task.status}`}>
            <div>
              <strong>{task.title}</strong> <span className="state-pill">{task.status.replace("_", " ")}</span>
            </div>
            <div className="muted small">
              {matrixRow(task.taskType)?.title ?? task.taskType} · R: {task.responsible.map(roleTitle).join(", ")} · A: {roleTitle(task.accountable)}
              {task.gate ? ` · ${GATE_TITLES[task.gate]} gate` : ""}
              {task.dependsOn.length ? ` · after ${task.dependsOn.map((id) => view.tasks.find((other) => other.id === id)?.title ?? id).join(", ")}` : ""}
            </div>
            <details>
              <summary>Contract and evidence</summary>
              <p>Deliverable: {task.deliverable}</p>
              <p>Acceptance criteria:</p>
              <ul>
                {task.acceptanceCriteria.map((criterion) => (
                  <li key={criterion}>{criterion}</li>
                ))}
              </ul>
              <p>Evidence required: {task.evidenceRequired.join("; ")}</p>
              {task.resources.length ? <p>Owns: {task.resources.join(", ")}</p> : null}
              <ul>
                {task.evidence.map((entry) => (
                  <li key={`${entry.at}:${entry.summary}`}>
                    {roleTitle(entry.role)} ({formatTime(entry.at)}): {entry.summary}
                    {entry.references.length ? ` [${entry.references.join(", ")}]` : ""}
                  </li>
                ))}
              </ul>
            </details>
            {task.accountable === "po" && task.status !== "done" && task.status !== "cancelled" ? (
              <button
                type="button"
                onClick={() =>
                  void officeApi
                    .completeTask(view.branch.id, task.id, "Accepted by the PO.")
                    .then(onChanged)
                    .catch((caught: Error) => setNotice(caught.message))
                }
              >
                Accept as done (you are accountable)
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}

function Roles({ view, onChanged }: WorkflowPanelProps) {
  const [notice, setNotice] = useState<string | null>(null);
  const live = view.roleSessions.filter((link) => link.retiredAt === null);
  return (
    <section aria-label="Roles" className="workflow-section">
      <h3>Roles</h3>
      <ul>
        {AGENT_ROLES.map((role) => {
          const links = live.filter((link) => link.role === role);
          return (
            <li key={role}>
              {roleTitle(role)}: {links.length ? `${links.length} session${links.length > 1 ? "s" : ""}` : "not started"}{" "}
              {links.some((link) => !link.temporary) ? (
                <button
                  type="button"
                  onClick={() =>
                    void officeApi
                      .rollover(view.branch.id, role, "PO restarted the role.")
                      .then(() => {
                        setNotice(`${roleTitle(role)} restarted with a recorded handoff.`);
                        onChanged();
                      })
                      .catch((caught: Error) => setNotice(caught.message))
                  }
                >
                  Restart with handoff
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
      <button
        type="button"
        className="danger"
        onClick={() =>
          void officeApi
            .stopBranch(view.branch.id)
            .then((result) => setNotice(`Stop sent to ${result.stopped} session(s).`))
            .catch((caught: Error) => setNotice(caught.message))
        }
      >
        Stop all work in this room
      </button>
      {notice ? <p role="status">{notice}</p> : null}
    </section>
  );
}

export function WorkflowPanel({ view, onChanged }: WorkflowPanelProps) {
  return (
    <div className="workflow-panel">
      <BoardEditor view={view} onChanged={onChanged} />
      <Interview view={view} onChanged={onChanged} />
      <Tasks view={view} onChanged={onChanged} />
      <Roles view={view} onChanged={onChanged} />
      {view.decisions.length ? (
        <section aria-label="Coordinator decisions" className="workflow-section">
          <h3>Coordinator decisions</h3>
          <ul>
            {view.decisions.map((decision) => (
              <li key={decision.id}>
                {formatTime(decision.at)}: {decision.summary}
                {decision.affectedRoles.length ? ` (affects ${decision.affectedRoles.map(roleTitle).join(", ")})` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <section aria-label="Event feed" className="workflow-section">
        <h3>Event feed</h3>
        <ol className="event-feed">
          {view.events.map((event) => (
            <li key={event.id}>
              <span className="muted small">{formatTime(event.at)}</span> {event.summary}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
