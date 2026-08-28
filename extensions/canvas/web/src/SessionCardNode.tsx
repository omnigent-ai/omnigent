import { memo, type KeyboardEvent } from "react";
import type { ExtensionSessionSummary } from "@omnigent/extension-sdk";
import type { Node, NodeProps } from "@xyflow/react";

export type SessionCardData = {
  session: ExtensionSessionSummary;
  onOpen: (sessionId: string) => void;
} & Record<string, unknown>;

function statusLabel(status: ExtensionSessionSummary["status"]): string {
  switch (status) {
    case "running":
      return "Running";
    case "waiting":
      return "Waiting";
    case "failed":
      return "Failed";
    default:
      return "Idle";
  }
}

function SessionCardNodeComponent({
  data,
  selected,
}: NodeProps<Node<SessionCardData>>) {
  const { session, onOpen } = data;
  const title = session.title?.trim() || "Untitled session";
  const workspace = session.workspace?.trim() || "No working directory";
  const status = statusLabel(session.status);
  const openFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    onOpen(session.id);
  };
  return (
    <div
      className={`session-card ${selected ? "session-card-selected" : ""}`}
      data-status={session.status}
      role="button"
      tabIndex={0}
      aria-label={`${title}. ${status}. ${workspace}`}
      onClick={(event) => {
        if (event.detail === 0) onOpen(session.id);
      }}
      onKeyDown={openFromKeyboard}
    >
      <div className="session-card-title-row">
        <span className="session-status-dot" aria-hidden />
        <strong title={title}>{title}</strong>
      </div>
      <span className="session-status-text">{status}</span>
      <span className="session-workspace" title={workspace}>
        {workspace}
      </span>
    </div>
  );
}

export const SessionCardNode = memo(SessionCardNodeComponent);
