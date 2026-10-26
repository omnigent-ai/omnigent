import { useEffect, useRef, useState } from "react";
import { OfficeApiError, type PendingElicitation, officeApi } from "../api.ts";

export interface ApprovalRequest {
  sessionId: string;
  workerName: string;
  elicitation: PendingElicitation;
}

interface ApprovalDialogProps {
  request: ApprovalRequest;
  onClose: () => void;
  onDecided: () => void;
}

/** Supporting detail for the request, without the fields already shown as the message. */
function requestDetail(elicitation: PendingElicitation): string | null {
  const { message: _message, ...rest } = elicitation.params ?? {};
  return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : null;
}

/** Answer an Omnigent approval request; feedback is only sent with a decline. */
export async function decideApproval(request: ApprovalRequest, action: "accept" | "decline", feedback = ""): Promise<void> {
  await officeApi.decide(request.sessionId, request.elicitation.elicitation_id, action, action === "decline" ? feedback : undefined);
}

export function ApprovalDialog({ request, onClose, onDecided }: ApprovalDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  const decide = async (action: "accept" | "decline") => {
    setBusy(true);
    setError(null);
    try {
      await decideApproval(request, action, feedback);
      onDecided();
    } catch (caught) {
      const message = (caught as Error).message;
      setError(caught instanceof OfficeApiError && caught.code === "stale_request" ? `${message} Close this dialog to see current requests.` : message);
    } finally {
      setBusy(false);
    }
  };

  const detail = requestDetail(request.elicitation);
  return (
    <dialog
      ref={dialogRef}
      className="approval-dialog"
      aria-labelledby="approval-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id="approval-title">Request from {request.workerName}</h2>
      <p className="approval-message">{request.elicitation.params?.message ?? "The worker is waiting for your decision."}</p>
      {detail ? (
        <details open>
          <summary>Supporting detail</summary>
          <pre>{detail}</pre>
        </details>
      ) : null}
      <p className="muted small">
        Approving answers this workflow request only. Omnigent runtime policies still decide whether the action may run.
      </p>
      <label htmlFor="approval-feedback">Feedback (sent with “Request changes”)</label>
      <textarea id="approval-feedback" rows={3} value={feedback} onChange={(event) => setFeedback(event.target.value)} />
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
      <div className="dialog-actions">
        <button type="button" onClick={onClose} disabled={busy}>
          Close
        </button>
        <button type="button" className="danger" onClick={() => void decide("decline")} disabled={busy}>
          Request changes
        </button>
        <button type="button" className="primary" onClick={() => void decide("accept")} disabled={busy}>
          Approve
        </button>
      </div>
    </dialog>
  );
}
