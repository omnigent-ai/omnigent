import { useEffect, useRef } from "react";
import type { BranchView } from "../../../shared/workflowTypes.ts";
import { BoardEditor, Interview, Tasks } from "./WorkflowSections.tsx";

export type BoardTab = "requirements" | "tasks" | "interview";

const BOARD_TABS: { id: BoardTab; title: string }[] = [
  { id: "requirements", title: "Requirements" },
  { id: "tasks", title: "Tasks" },
  { id: "interview", title: "PM interview" },
];

interface BoardModalProps {
  view: BranchView;
  tab: BoardTab;
  onTabChange: (tab: BoardTab) => void;
  onChanged: () => void;
  onClose: () => void;
}

/** The current board: requirements, the task graph, and the PM interview. */
export function BoardModal({ view, tab, onTabChange, onChanged, onClose }: BoardModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openTasks = view.tasks.filter((task) => task.status !== "done" && task.status !== "cancelled").length;
  const question = view.interviews.some((entry) => entry.status === "asked");
  const counts: Record<BoardTab, string | null> = {
    requirements: view.board.requirements.length ? String(view.board.requirements.length) : null,
    tasks: openTasks ? String(openTasks) : null,
    interview: question ? "1" : null,
  };

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal?.();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="approval-dialog board-modal"
      aria-labelledby="board-modal-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header className="board-modal-header">
        <h2 id="board-modal-title">Board</h2>
        <button type="button" onClick={onClose} aria-label="Close board">
          ✕
        </button>
      </header>
      <div role="tablist" aria-label="Board" className="workflow-tabs">
        {BOARD_TABS.map((entry) => (
          <button
            key={entry.id}
            id={`board-tab-${entry.id}`}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            aria-controls="board-tabpanel"
            className="workflow-tab"
            onClick={() => onTabChange(entry.id)}
          >
            {entry.title}
            {counts[entry.id] ? (
              <span className={`tab-badge${entry.id === "interview" ? " tab-badge-urgent" : ""}`}>{counts[entry.id]}</span>
            ) : null}
          </button>
        ))}
      </div>
      <div role="tabpanel" id="board-tabpanel" aria-labelledby={`board-tab-${tab}`} className="workflow-tabpanel">
        {tab === "requirements" ? <BoardEditor view={view} onChanged={onChanged} /> : null}
        {tab === "tasks" ? <Tasks view={view} onChanged={onChanged} /> : null}
        {tab === "interview" ? <Interview view={view} onChanged={onChanged} /> : null}
      </div>
    </dialog>
  );
}
