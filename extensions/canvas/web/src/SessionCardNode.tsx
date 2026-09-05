import { memo, type KeyboardEvent, type MouseEvent } from "react";
import type {
  ExtensionPullRequest,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";
import type { Node, NodeProps } from "@xyflow/react";

export type SessionCardData = {
  session: ExtensionSessionSummary;
  pullRequest?: ExtensionPullRequest | null;
  onOpen: (sessionId: string) => void;
  onOpenExternal?: (url: string) => void;
} & Record<string, unknown>;

// Mirrors the shell sidebar: a spinner while the agent works, the brand-pink
// dot for finished output the user has not seen yet, and a gray dot otherwise.
type CardState = "running" | "waiting" | "unread" | "idle";

const STATE_LABELS: Record<CardState, string> = {
  running: "Running",
  waiting: "Waiting",
  unread: "New messages",
  idle: "Idle",
};

function cardState(session: ExtensionSessionSummary): CardState {
  if (session.status === "running" || session.status === "waiting") {
    return session.status;
  }
  return session.unread ? "unread" : "idle";
}

function StateIndicator({ state }: { state: CardState }) {
  if (state === "running" || state === "waiting") {
    return (
      <svg
        className="session-spinner"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M21 12a9 9 0 1 1-6.219-8.56" />
      </svg>
    );
  }
  return <span className="session-status-dot" aria-hidden />;
}

// Same lucide glyphs the shell's new-session chips use.
const ICON_PROPS = {
  className: "session-meta-icon",
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

function FolderIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
    </svg>
  );
}

function GitBranchIcon() {
  return (
    <svg {...ICON_PROPS}>
      <line x1="6" x2="6" y1="3" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  );
}

function GithubIcon() {
  return (
    <svg {...ICON_PROPS}>
      <path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4" />
      <path d="M9 18c-4.51 2-5-2-7-2" />
    </svg>
  );
}

function SessionCardNodeComponent({
  data,
  selected,
}: NodeProps<Node<SessionCardData>>) {
  const { session, pullRequest, onOpen, onOpenExternal } = data;
  const title = session.title?.trim() || "Untitled session";
  const workspace = session.workspace?.trim() || "No working directory";
  const state = cardState(session);
  const stateLabel = STATE_LABELS[state];
  const openFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    onOpen(session.id);
  };
  const openPullRequest = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (pullRequest) onOpenExternal?.(pullRequest.url);
  };
  return (
    <div
      className={`session-card ${selected ? "session-card-selected" : ""}`}
      data-state={state}
      role="button"
      tabIndex={0}
      aria-label={`${title}. ${stateLabel}. ${workspace}`}
      onClick={(event) => {
        if (event.detail === 0) onOpen(session.id);
      }}
      onKeyDown={openFromKeyboard}
    >
      <div className="session-card-title-row">
        <StateIndicator state={state} />
        <strong
          title={title}
          className={
            session.titleProvisional
              ? "session-card-title-provisional"
              : undefined
          }
        >
          {title}
        </strong>
      </div>
      <span className="session-status-text">{stateLabel}</span>
      <span className="session-meta session-workspace" title={workspace}>
        <FolderIcon />
        <span className="session-meta-text">{workspace}</span>
      </span>
      {session.gitBranch && (
        <span className="session-meta session-branch" title={session.gitBranch}>
          <GitBranchIcon />
          <span className="session-meta-text">{session.gitBranch}</span>
        </span>
      )}
      {pullRequest && (
        <button
          type="button"
          className="session-meta session-pull-request"
          title={`${pullRequest.title} (${pullRequest.state.toLowerCase()})`}
          aria-label={`Open pull request #${pullRequest.number}`}
          onClick={openPullRequest}
          onDoubleClick={(event) => event.stopPropagation()}
        >
          <GithubIcon />
          <span className="session-meta-text">
            #{pullRequest.number} {pullRequest.title}
          </span>
        </button>
      )}
    </div>
  );
}

export const SessionCardNode = memo(SessionCardNodeComponent);
