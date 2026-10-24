import { useEffect, useRef, useState } from "react";
import { GATE_TITLES, roleTitle } from "../../../shared/sdlcTheme.ts";
import type { BoardProposal, GateRequest, RequirementsBoard, WorkflowTask } from "../../../shared/workflowTypes.ts";
import { OfficeApiError, officeApi } from "../../api.ts";

export type WorkflowRequest =
  | { kind: "gate"; gate: GateRequest; task: WorkflowTask | null }
  | { kind: "proposal"; proposal: BoardProposal; board: RequirementsBoard };

interface WorkflowRequestDialogProps {
  branchId: string;
  request: WorkflowRequest;
  onClose: () => void;
  onDecided: () => void;
}

function RequirementsList({ requirements }: { requirements: BoardProposal["requirements"] }) {
  return (
    <ol className="requirements-preview">
      {requirements.map((requirement) => (
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
      ))}
    </ol>
  );
}

export function WorkflowRequestDialog({ branchId, request, onClose, onDecided }: WorkflowRequestDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const decide = async (approve: boolean) => {
    setBusy(true);
    setError(null);
    try {
      if (request.kind === "gate") {
        await officeApi.decideGate(branchId, request.gate.id, approve ? "approve" : "request_changes", request.gate.artifact.digest, feedback);
      } else {
        await officeApi.decideProposal(branchId, request.proposal.id, approve ? "accept" : "reject", feedback);
      }
      onDecided();
    } catch (caught) {
      const message = (caught as Error).message;
      setError(caught instanceof OfficeApiError && caught.code === "stale_request" ? `${message} Close this dialog to see current requests.` : message);
    } finally {
      setBusy(false);
    }
  };

  const title =
    request.kind === "gate"
      ? `${GATE_TITLES[request.gate.gate]} approval requested by ${roleTitle(request.gate.requestedBy)}`
      : "PM proposal for the requirements board";
  return (
    <dialog
      ref={dialogRef}
      className="approval-dialog"
      aria-labelledby="workflow-request-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id="workflow-request-title">{title}</h2>
      {request.kind === "gate" ? (
        <>
          <p className="approval-message">{request.gate.summary}</p>
          <dl className="request-facts">
            {request.task ? (
              <>
                <dt>Task</dt>
                <dd>
                  {request.task.title} — {request.task.deliverable}
                </dd>
              </>
            ) : null}
            {request.gate.operation ? (
              <>
                <dt>Operation this approval authorizes</dt>
                <dd>
                  <code>{request.gate.operation}</code>
                </dd>
              </>
            ) : null}
            {request.gate.targetEnvironment ? (
              <>
                <dt>Target environment</dt>
                <dd>{request.gate.targetEnvironment}</dd>
              </>
            ) : null}
            <dt>Artifact</dt>
            <dd>
              {request.gate.artifact.kind}: {request.gate.artifact.ref}
            </dd>
            <dt>Artifact digest</dt>
            <dd>
              <code>{request.gate.artifact.digest}</code>
            </dd>
            <dt>Gate policy revision</dt>
            <dd>
              <code>{request.gate.gatePolicyRevision}</code>
            </dd>
          </dl>
          {request.task?.acceptanceCriteria.length ? (
            <details open>
              <summary>Acceptance criteria</summary>
              <ul>
                {request.task.acceptanceCriteria.map((criterion) => (
                  <li key={criterion}>{criterion}</li>
                ))}
              </ul>
            </details>
          ) : null}
          {request.task?.evidence.length ? (
            <details>
              <summary>Evidence ({request.task.evidence.length})</summary>
              <ul>
                {request.task.evidence.map((entry) => (
                  <li key={`${entry.at}:${entry.summary}`}>
                    {roleTitle(entry.role)}: {entry.summary} {entry.references.length ? `(${entry.references.join(", ")})` : ""}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : (
        <>
          {request.proposal.rationale ? <p className="approval-message">{request.proposal.rationale}</p> : null}
          <p className="muted small">
            Proposed against board revision {request.proposal.baseRevision} (current: {request.board.revision}).
          </p>
          <RequirementsList requirements={request.proposal.requirements} />
        </>
      )}
      <p className="muted small">
        Approving satisfies this workflow step only. It grants no credentials; Omnigent runtime policies still decide whether any action may run.
      </p>
      <label htmlFor="workflow-feedback">Feedback (required to request changes)</label>
      <textarea id="workflow-feedback" rows={3} value={feedback} onChange={(event) => setFeedback(event.target.value)} />
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
      <div className="dialog-actions">
        <button type="button" onClick={onClose} disabled={busy}>
          Close
        </button>
        <button
          type="button"
          className="danger"
          onClick={() => void decide(false)}
          disabled={busy || (request.kind === "gate" && !feedback.trim())}
        >
          {request.kind === "gate" ? "Request changes" : "Reject"}
        </button>
        <button type="button" className="primary" onClick={() => void decide(true)} disabled={busy}>
          {request.kind === "gate" ? "Approve" : "Accept"}
        </button>
      </div>
    </dialog>
  );
}
