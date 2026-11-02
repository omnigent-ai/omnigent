// A collapsed side pane: a thin rail that keeps the pane one click away.

interface PaneRailProps {
  side: "left" | "right";
  label: string;
  /** A short count or status shown on the rail, e.g. "11". */
  badge?: string | null;
  onExpand: () => void;
}

export function PaneRail({ side, label, badge, onExpand }: PaneRailProps) {
  return (
    <aside className={`room-pane pane-rail pane-rail-${side}`} aria-label={`${label} (collapsed)`}>
      <button type="button" className="pane-rail-button" aria-expanded={false} aria-label={`Expand ${label}`} title={`Expand ${label}`} onClick={onExpand}>
        <span aria-hidden="true">{side === "left" ? "»" : "«"}</span>
        <span className="pane-rail-label">{label}</span>
        {badge ? <span className="pane-rail-badge">{badge}</span> : null}
      </button>
    </aside>
  );
}

/** The button in an open pane's header that collapses it to a rail. */
export function CollapseButton({ side, label, onCollapse }: { side: "left" | "right"; label: string; onCollapse: () => void }) {
  return (
    <button type="button" className="pane-collapse" aria-expanded={true} aria-label={`Collapse ${label}`} title={`Collapse ${label}`} onClick={onCollapse}>
      {side === "left" ? "«" : "»"}
    </button>
  );
}
