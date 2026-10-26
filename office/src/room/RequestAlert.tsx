import { useState, type ReactNode } from "react";
import { OfficeApiError } from "../api.ts";

interface RequestAlertProps {
  label: ReactNode;
  /** Shown under the label so a one-click approval is still an informed one, e.g. the command it allows. */
  detail?: ReactNode;
  approveLabel: string;
  onApprove: () => Promise<void>;
  onDetails: () => void;
  /** Called after a decision lands, or when the request turned out to be stale. */
  onChanged: () => void;
}

/** One pending approval: approve it in place, or open the full request in a dialog. */
export function RequestAlert({ label, detail, approveLabel, onApprove, onDetails, onChanged }: RequestAlertProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      await onApprove();
      onChanged();
    } catch (caught) {
      const stale = caught instanceof OfficeApiError && caught.code === "stale_request";
      setError(stale ? `${(caught as Error).message} The list was refreshed.` : (caught as Error).message);
      if (stale) onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="request-alert">
      <div className="request-alert-text">
        <span>⚑ {label}</span>
        {detail ? <span className="muted small request-alert-detail">{detail}</span> : null}
      </div>
      <div className="request-alert-actions">
        <button type="button" className="primary" disabled={busy} onClick={() => void approve()}>
          {busy ? "Approving…" : approveLabel}
        </button>
        <button type="button" disabled={busy} onClick={onDetails}>
          Details
        </button>
      </div>
      {error ? (
        <p className="error-text small" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
