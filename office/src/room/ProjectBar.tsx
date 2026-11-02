import { useState } from "react";
import { PRIORITY_LABELS, type RankExplanation } from "../../shared/ranking.ts";
import type { Priority, RoomSettingsPatch, RoomSummary } from "../../shared/types.ts";

export interface RoomStatus {
  label: string;
  tone: "ok" | "busy" | "attention" | "problem";
}

interface ProjectBarProps {
  room: RoomSummary;
  themed: boolean;
  status: RoomStatus;
  /** You plus every current session in the room. */
  partyCount: number;
  eventCount: string | null;
  explanation: RankExplanation;
  onExit: () => void;
  onOpenBoard: (() => void) | null;
  onOpenTeam: () => void;
  onUpdateRoom: (patch: RoomSettingsPatch) => Promise<void>;
}

/** The room's project at a glance, with the ways out and in to its board and team. */
export function ProjectBar({
  room,
  themed,
  status,
  partyCount,
  eventCount,
  explanation,
  onExit,
  onOpenBoard,
  onOpenTeam,
  onUpdateRoom,
}: ProjectBarProps) {
  const [showRank, setShowRank] = useState(false);
  return (
    <header className="project-bar">
      <div
        className="project-bar-title"
        title={themed ? "SDLC theme · you are the Product Owner · the PM coordinates" : "Unthemed project · each session works as a generalist"}
      >
        <h1>
          {room.icon ? `${room.icon} ` : ""}
          {room.name}
        </h1>
        <p className="muted small">{themed ? "SDLC · you are the PO" : "Unthemed · generalists"}</p>
      </div>
      <dl className="project-stats">
        <div>
          <dt>Status</dt>
          <dd className={`project-status project-status-${status.tone}`}>{status.label}</dd>
        </div>
        <div>
          <dt>Party</dt>
          <dd>{partyCount}</dd>
        </div>
        {eventCount !== null ? (
          <div>
            <dt>Events</dt>
            <dd>{eventCount}</dd>
          </div>
        ) : null}
      </dl>
      <div className="project-actions">
        {onOpenBoard ? (
          <button type="button" onClick={onOpenBoard}>
            Board
          </button>
        ) : null}
        <button type="button" onClick={onOpenTeam}>
          Team
        </button>
        <details className="room-settings">
          <summary>Room settings</summary>
          <div className="room-settings-body">
            <label>
              Priority{" "}
              <select value={room.priority} onChange={(event) => void onUpdateRoom({ priority: event.target.value as Priority })}>
                {(Object.keys(PRIORITY_LABELS) as Priority[]).map((priority) => (
                  <option key={priority} value={priority}>
                    {PRIORITY_LABELS[priority]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input type="checkbox" checked={room.keepUpstairs} onChange={(event) => void onUpdateRoom({ keepUpstairs: event.target.checked })} /> Keep
              upstairs
            </label>
            <button type="button" aria-expanded={showRank} onClick={() => setShowRank((shown) => !shown)}>
              Explain rank
            </button>
            {showRank ? (
              <p className="rank-explanation" role="note">
                <strong>{explanation.headline}.</strong> {explanation.details.join(" ")}
              </p>
            ) : null}
          </div>
        </details>
        <button type="button" className="exit-button" onClick={onExit}>
          ⬅ Exit office
        </button>
      </div>
    </header>
  );
}
