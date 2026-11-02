import { Fragment, useState, type ReactNode } from "react";
import { roleTitle } from "../../../shared/sdlcTheme.ts";
import { ACTION_LABELS, READINESS_LABELS, type RecoveryAction, type RoleHealth } from "../../../shared/troubleshoot.ts";
import type { BranchView } from "../../../shared/workflowTypes.ts";
import { officeApi } from "../../api.ts";
import { useRuntimeOptions } from "../../capabilities.ts";
import { SwitchHarnessDialog } from "./SwitchHarnessDialog.tsx";

const RETRY_DELAYS = [5, 15, 30, 60] as const;

export function problemSubject(branchId: string, entry: RoleHealth): string {
  return entry.sessionId ?? `${branchId}:${entry.role}`;
}

export function problemAnchor(branchId: string, entry: RoleHealth): string {
  return `problem-${problemSubject(branchId, entry).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

/** Health entries with a problem, undismissed first. */
export function openProblems(view: BranchView): RoleHealth[] {
  return (view.health ?? []).filter((entry) => entry.problem && !entry.dismissed);
}

function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** Render `command` spans from remediation text as code. */
function withCode(text: string): ReactNode {
  return text.split(/(`[^`]+`)/).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? <code key={index}>{part.slice(1, -1)}</code> : <Fragment key={index}>{part}</Fragment>,
  );
}

function roleLabel(entry: RoleHealth): string {
  return `${roleTitle(entry.role)}${entry.temporary ? " (temporary)" : ""}`;
}

interface ProblemCardProps {
  view: BranchView;
  entry: RoleHealth;
  onChanged: () => void;
  onSwitch: (entry: RoleHealth) => void;
}

function ProblemCard({ view, entry, onChanged, onSwitch }: ProblemCardProps) {
  const problem = entry.problem;
  const omnigentUrl = useRuntimeOptions()?.omnigentUrl;
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [delay, setDelay] = useState<number>(15);
  const [repeat, setRepeat] = useState(true);
  const [schedulingOpen, setSchedulingOpen] = useState(false);
  if (!problem) return null;
  const branchId = view.branch.id;
  const sessionId = entry.sessionId;

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setNotice(null);
    try {
      setNotice(await action());
      onChanged();
    } catch (caught) {
      setNotice((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const perform = (action: RecoveryAction) => {
    if (action === "switch_harness") return onSwitch(entry);
    if (action === "retry_later") return setSchedulingOpen((open) => !open);
    if (action === "restart" || action === "start") {
      return void run(async () => {
        await officeApi.rollover(branchId, entry.role, `${problem.title}: ${problem.detail}`.slice(0, 2000));
        return action === "start" ? `${roleTitle(entry.role)} started.` : `${roleTitle(entry.role)} restarted with a recorded handoff.`;
      });
    }
    if (!sessionId) return;
    void run(async () => (await officeApi.recoverSession(branchId, sessionId, action)).result);
  };

  const retry = entry.scheduledRetry;
  const notReady = entry.harnessReadiness !== "ready" && entry.harnessReadiness !== "unknown";

  return (
    <li id={problemAnchor(branchId, entry)} className={`agent-problem${entry.dismissed ? " agent-problem-dismissed" : ""}`} tabIndex={-1}>
      <h4>
        ⚠ {roleLabel(entry)}: {problem.title}
      </h4>
      <p className="muted small">
        {entry.harness}
        {entry.model ? ` · ${entry.model}` : ""}
        {notReady ? ` · on this host: ${READINESS_LABELS[entry.harnessReadiness]}` : ""}
        {problem.since ? ` · since ${formatTime(problem.since)}` : ""}
        {problem.code ? ` · ${problem.code}` : ""}
      </p>
      <p className="agent-problem-detail">{problem.detail}</p>
      {problem.cause ? <p className="small">Why: {problem.cause}</p> : null}
      {problem.remediation ? <p className="small">How to fix: {withCode(problem.remediation)}</p> : null}
      {retry ? (
        <p className="small" role="status">
          Retry scheduled for {formatTime(retry.nextAt)}
          {retry.repeatMinutes ? `, then every ${retry.repeatMinutes} min until it recovers` : ""}
          {retry.attempts ? ` · ${retry.attempts} attempt${retry.attempts === 1 ? "" : "s"} so far` : ""}
          {retry.lastResult ? ` · last: ${retry.lastResult}` : ""}{" "}
          <button
            type="button"
            disabled={busy}
            onClick={() => sessionId && void run(async () => (await officeApi.scheduleRetry(branchId, sessionId, null, false), "Scheduled retry cancelled."))}
          >
            Cancel retry
          </button>
        </p>
      ) : null}
      <div className="agent-problem-actions">
        {problem.actions.map((action, index) => (
          <button
            key={action}
            type="button"
            className={index === 0 ? "primary" : undefined}
            disabled={busy || (action === "retry_later" && !sessionId)}
            aria-expanded={action === "retry_later" ? schedulingOpen : undefined}
            onClick={() => perform(action)}
          >
            {ACTION_LABELS[action]}
          </button>
        ))}
        {sessionId && omnigentUrl ? (
          <a href={`${omnigentUrl.replace(/\/$/, "")}/c/${encodeURIComponent(sessionId)}`} target="_blank" rel="noreferrer">
            Open in Omnigent
          </a>
        ) : null}
        {entry.dismissed ? null : (
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await officeApi.dismissProblem(branchId, problemSubject(branchId, entry), problem.key);
                return "Ignored. A new failure will show here again.";
              })
            }
          >
            Ignore this problem
          </button>
        )}
      </div>
      {schedulingOpen && sessionId ? (
        <form
          className="agent-problem-schedule"
          onSubmit={(event) => {
            event.preventDefault();
            setSchedulingOpen(false);
            void run(async () => {
              await officeApi.scheduleRetry(branchId, sessionId, delay, repeat);
              return `Retry scheduled in ${delay} minutes${repeat ? ", repeating until it recovers" : ""}. It runs even with the browser closed.`;
            });
          }}
        >
          <label>
            Retry in{" "}
            <select value={delay} onChange={(event) => setDelay(Number(event.target.value))}>
              {RETRY_DELAYS.map((minutes) => (
                <option key={minutes} value={minutes}>
                  {minutes} minutes
                </option>
              ))}
            </select>
          </label>
          <label>
            <input type="checkbox" checked={repeat} onChange={(event) => setRepeat(event.target.checked)} /> Keep retrying until it recovers
          </label>
          <button type="submit" disabled={busy}>
            Schedule retry
          </button>
        </form>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
    </li>
  );
}

/** Failing or stuck agents, with the ways to get each one going again. */
export function AgentProblems({ view, onChanged }: { view: BranchView; onChanged: () => void }) {
  const [switching, setSwitching] = useState<RoleHealth | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const health = view.health ?? [];
  const open = health.filter((entry) => entry.problem && !entry.dismissed);
  const ignored = health.filter((entry) => entry.problem && entry.dismissed);
  if (open.length === 0 && ignored.length === 0 && !notice) return null;

  return (
    <section aria-label="Agent problems" className="workflow-section agent-problems">
      <h3>Agent problems</h3>
      {open.length ? (
        <ul className="agent-problem-list">
          {open.map((entry) => (
            <ProblemCard key={problemSubject(view.branch.id, entry)} view={view} entry={entry} onChanged={onChanged} onSwitch={setSwitching} />
          ))}
        </ul>
      ) : (
        <p className="muted">No agent problems need you right now.</p>
      )}
      {ignored.length ? (
        <details>
          <summary>Ignored problems ({ignored.length})</summary>
          <ul className="agent-problem-list">
            {ignored.map((entry) => (
              <ProblemCard key={problemSubject(view.branch.id, entry)} view={view} entry={entry} onChanged={onChanged} onSwitch={setSwitching} />
            ))}
          </ul>
        </details>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {switching ? (
        <SwitchHarnessDialog
          branchId={view.branch.id}
          hostId={view.branch.runtime.hostId}
          target={switching}
          roles={health}
          preferOtherHarness
          reason={switching.problem ? `${switching.problem.title}: ${switching.problem.detail}`.slice(0, 2000) : "PO switched the harness."}
          onClose={() => setSwitching(null)}
          onSwitched={(message) => {
            setSwitching(null);
            setNotice(message);
            onChanged();
          }}
        />
      ) : null}
    </section>
  );
}
