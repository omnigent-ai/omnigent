import { INTERVIEW_SEGMENTS, interviewProgress } from "../../../shared/interviewProgress.ts";
import type { InterviewQuestion } from "../../../shared/workflowTypes.ts";

/** One bar split into answered, awaiting, to be answered, newly added, and skipped questions. */
export function InterviewProgressBar({ questions }: { questions: InterviewQuestion[] }) {
  const progress = interviewProgress(questions);
  if (progress.total === 0) return <p className="muted small">The PM has not planned any questions yet.</p>;
  const shown = INTERVIEW_SEGMENTS.filter((segment) => progress.counts[segment.id] > 0);
  const summary = shown.map((segment) => `${progress.counts[segment.id]} ${segment.label.toLowerCase()}`).join(", ");
  const resolved = progress.counts.answered + progress.counts.skipped;

  return (
    <div className="interview-progress">
      <p className="small">
        {progress.complete ? "Interview complete: " : ""}
        {resolved} of {progress.total} questions done
      </p>
      <div className="interview-progress-bar" role="img" aria-label={`Interview progress: ${summary}`}>
        {shown.map((segment) => (
          <span
            key={segment.id}
            className={`interview-segment interview-segment-${segment.id}`}
            style={{ flexGrow: progress.counts[segment.id] }}
            title={`${segment.label}: ${progress.counts[segment.id]}`}
          />
        ))}
      </div>
      <ul className="interview-legend small">
        {INTERVIEW_SEGMENTS.map((segment) => (
          <li key={segment.id}>
            <span className={`interview-swatch interview-segment-${segment.id}`} aria-hidden="true" /> {segment.label}{" "}
            <strong>{progress.counts[segment.id]}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}
