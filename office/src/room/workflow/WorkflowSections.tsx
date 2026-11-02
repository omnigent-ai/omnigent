// The SDLC workflow's sections: requirements, interview, tasks, activity, and
// the per-role pieces the Team pane shows.

import { useEffect, useState } from "react";
import type { RoleToolReport } from "../../../shared/roleTools.ts";
import type { RoleWorkLine } from "../../../shared/roleWork.ts";
import { GATE_TITLES, roleTitle } from "../../../shared/sdlcTheme.ts";
import { taskTypeOf, teamOf, coordinatorName } from "../../../shared/team.ts";
import type { BranchView, Requirement } from "../../../shared/workflowTypes.ts";
import { OfficeApiError, officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { DictationButton } from "../../Dictation.tsx";
import { interviewSegment, INTERVIEW_SEGMENTS } from "../../../shared/interviewProgress.ts";
import type { RoleHealth } from "../../../shared/troubleshoot.ts";
import { problemAnchor } from "./AgentProblems.tsx";
import { InterviewProgressBar } from "./InterviewProgressBar.tsx";

export interface WorkflowPanelProps {
  view: BranchView;
  onChanged: () => void;
}

const FEED_PAGE = 30;

/** The role's own (persistent) session health, if the room loaded it. */
export function roleHealth(view: BranchView, role: string): RoleHealth | undefined {
  return view.health?.find((entry) => entry.role === role && !entry.temporary);
}

export function focusProblem(view: BranchView, entry: RoleHealth): void {
  const card = document.getElementById(problemAnchor(view.branch.id, entry));
  card?.scrollIntoView({ block: "center" });
  card?.focus();
}

function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function appendText(current: string, addition: string): string {
  return current.trim() ? `${current.trimEnd()} ${addition}` : addition;
}

export function BoardEditor({ view, onChanged }: WorkflowPanelProps) {
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
      {requirements.length === 0 && !editing ? <p className="muted">No requirements yet. Add them or start a {coordinatorName()} interview.</p> : null}
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

export function Interview({ view, onChanged }: WorkflowPanelProps) {
  const [topic, setTopic] = useState("");
  const [answer, setAnswer] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dictationAvailable = useRuntimeOptions()?.dictationAvailable ?? false;
  const open = view.interviews.find((question) => question.status === "asked") ?? null;
  const current = view.currentInterview;
  const agenda = current ? view.interviews.filter((question) => question.interviewId === current.id) : [];
  const segmentLabel = (question: (typeof agenda)[number]) =>
    INTERVIEW_SEGMENTS.find((segment) => segment.id === interviewSegment(question))?.label ?? "";
  const pm = roleHealth(view, "pm");
  const waitingOnPm = Boolean(current) && !open && (agenda.length === 0 || agenda.some((question) => question.status === "planned"));

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
    <section aria-label="Interview" className="workflow-section">
      <h3>{coordinatorName()} interview{current?.topic ? `: ${current.topic}` : ""}</h3>
      {current ? <InterviewProgressBar questions={agenda} /> : null}
      {agenda.length ? (
        <details>
          <summary>Interview agenda</summary>
          <ol className="interview-agenda">
            {agenda.map((question) => (
              <li key={question.id}>
                {question.question} <span className={`pill interview-pill-${interviewSegment(question)}`}>{segmentLabel(question)}</span>
                {question.skipReason ? <span className="muted small"> ({question.skipReason})</span> : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      {waitingOnPm ? (
        pm?.problem && !pm.dismissed ? (
          <p className="error-text" role="status">
            ⚠ The {coordinatorName()} can't continue the interview: {pm.problem.title}.{" "}
            <button type="button" onClick={() => focusProblem(view, pm)}>
              See how to fix it
            </button>
          </p>
        ) : (
          <p className="muted" role="status">
            {pm?.state === "working" ? `The ${coordinatorName()} is preparing its next question…` : `Waiting for the ${coordinatorName()} to ask its next question.`}
          </p>
        )
      ) : null}
      {open ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              await officeApi.answerInterview(view.branch.id, open.id, answer.trim());
              setAnswer("");
            }, `Answer sent to the ${coordinatorName()}.`);
          }}
        >
          <p className="approval-message">
            <strong>{coordinatorName()} asks:</strong> {open.question}
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
            void run(() => officeApi.startInterview(view.branch.id, topic), `Interview started; the ${coordinatorName()} was asked to plan its questions.`);
          }}
        >
          <label htmlFor="interview-topic">Interview topic (optional)</label>
          <input id="interview-topic" value={topic} onChange={(event) => setTopic(event.target.value)} />
          <button type="submit" disabled={busy}>
            Start {coordinatorName()} interview
          </button>
        </form>
      )}
      {view.interviews.some((question) => question.status === "answered") ? (
        <details>
          <summary>Answered questions</summary>
          <ol>
            {view.interviews
              .filter((question) => question.status === "answered")
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

export function Tasks({ view, onChanged }: WorkflowPanelProps) {
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <section aria-label="Tasks" className="workflow-section">
      <h3>Tasks</h3>
      {view.tasks.length === 0 ? <p className="muted">The {coordinatorName()} has not created tasks yet.</p> : null}
      <ul className="task-list">
        {view.tasks.map((task) => (
          <li key={task.id} className={`task task-${task.status}`}>
            <div>
              <strong>{task.title}</strong> <span className="state-pill">{task.status.replace("_", " ")}</span>
            </div>
            {task.status !== "done" && task.status !== "cancelled"
              ? task.responsible
                  .map((role) => roleHealth(view, role))
                  .filter((entry): entry is RoleHealth => Boolean(entry?.problem && !entry.dismissed))
                  .map((entry) => (
                    <p key={entry.role} className="error-text small">
                      ⚠ Waiting on the {roleTitle(entry.role)}, who is stuck: {entry.problem?.title}.{" "}
                      <button type="button" onClick={() => focusProblem(view, entry)}>
                        See how to fix it
                      </button>
                    </p>
                  ))
              : null}
            <div className="muted small">
              {taskTypeOf(teamOf(view.branch.runtime), task.taskType)?.title ?? task.taskType} · R: {task.responsible.map(roleTitle).join(", ")} · A: {roleTitle(task.accountable)}
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

const TOOL_SOURCE_NOTES: Record<RoleToolReport["source"], string> = {
  session: "As Omnigent reports them for the running session.",
  planned: "Not started yet: what the role will start with.",
  unavailable: "Omnigent did not report this session's configuration; showing what the office set up.",
};

/** Configured MCP servers, skills, and policies, and the office tools the role may call. */
export function RoleTools({ report }: { report: RoleToolReport }) {
  return (
    <div className="role-tools">
      <p className="muted small">
        {TOOL_SOURCE_NOTES[report.source]} The harness's own built-in tools (shell, file edits, search) come on top.
      </p>
      <div className="role-tools-grid">
        <section aria-label="Available office tools">
          <h4>Available office tools ({report.officeTools.length})</h4>
          <ul className="tool-chips">
            {report.officeTools.map((tool) => (
              <li key={tool.name} title={tool.description}>
                <code>{tool.name}</code>
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="Configured MCP servers">
          <h4>MCP servers ({report.mcpServers.length})</h4>
          <ul>
            {report.mcpServers.map((server) => (
              <li key={server.name}>
                <code>{server.name}</code>
                {server.transport ? <span className="muted"> · {server.transport}</span> : null}
                {server.target ? <div className="muted small tool-target">{server.target}</div> : null}
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="Configured skills">
          <h4>Skills ({report.skills.length})</h4>
          {report.skills.length ? (
            <ul>
              {report.skills.map((skill) => (
                <li key={skill}>
                  <code>{skill}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">None</p>
          )}
        </section>
        <section aria-label="Configured policies">
          <h4>Policies ({report.policies.length})</h4>
          <ul>
            {report.policies.map((policy) => (
              <li key={policy.name}>
                <code>{policy.name}</code>
                {policy.description ? <div className="muted small">{policy.description}</div> : null}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

const WORK_POLL_MS = 5_000;

export function timeAgo(epochSeconds: number): string {
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}

/** Each role's one-line current work, refreshed while the Team tab is open. */
export function useRoleWork(branchId: string | null): Map<string, RoleWorkLine> {
  const [lines, setLines] = useState<Map<string, RoleWorkLine>>(new Map());
  useEffect(() => {
    if (!branchId) return;
    let cancelled = false;
    const load = () =>
      officeApi
        .roleWork(branchId)
        .then((data) => !cancelled && setLines(new Map(data.map((entry) => [entry.role, entry]))))
        .catch(() => undefined);
    void load();
    const timer = window.setInterval(load, WORK_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [branchId]);
  return lines;
}

export function Activity({ view }: { view: BranchView }) {
  const [shownEvents, setShownEvents] = useState(FEED_PAGE);
  const [allDecisions, setAllDecisions] = useState(false);
  const decisions = [...view.decisions].reverse();
  const visibleDecisions = allDecisions ? decisions : decisions.slice(0, 5);
  return (
    <>
      {decisions.length ? (
        <section aria-label="Coordinator decisions" className="workflow-section">
          <h3>Coordinator decisions <span className="muted small">newest first</span></h3>
          <ul className="decision-list">
            {visibleDecisions.map((decision) => (
              <li key={decision.id}>
                <span className="muted small">{formatTime(decision.at)}</span> {decision.summary}
                {decision.affectedRoles.length ? <span className="muted small"> (affects {decision.affectedRoles.map(roleTitle).join(", ")})</span> : null}
              </li>
            ))}
          </ul>
          {decisions.length > 5 ? (
            <button type="button" onClick={() => setAllDecisions((shown) => !shown)}>
              {allDecisions ? "Show the latest 5" : `Show all ${decisions.length} decisions`}
            </button>
          ) : null}
        </section>
      ) : null}
      <section aria-label="Event feed" className="workflow-section">
        <h3>Event feed <span className="muted small">newest first</span></h3>
        <ol className="event-feed">
          {view.events.slice(0, shownEvents).map((event) => (
            <li key={event.id}>
              <span className="muted small">{formatTime(event.at)}</span> {event.summary}
            </li>
          ))}
        </ol>
        {view.events.length > shownEvents ? (
          <button type="button" onClick={() => setShownEvents((count) => count + FEED_PAGE)}>
            Show older events
          </button>
        ) : null}
      </section>
    </>
  );
}
