// How long one opening of the Pull Requests panel takes to answer, as durations
// only: no repository, pull request, or account ever enters a report.

/** Which path answered: the session host's provider CLI, or the desktop app itself. */
export type PullRequestReadPath = "host" | "desktop";

/** Milestones after the panel asks for its info, in the order a PR view usually reaches them. */
export type PullRequestTimingMilestone =
  "state" | "header" | "patchRequested" | "diff" | "changesShown" | "diffShown";

export interface PullRequestTimingReport {
  sessionId: string;
  path: PullRequestReadPath;
  /** The panel state the info settled into, e.g. "ready" or "repo-unresolved". */
  outcome: string;
  /** Info was already cached when the panel opened, so `stateMs` is near zero. */
  cached: boolean;
  /** Until the panel left its loading state. */
  stateMs: number;
  /** Until a pull request's header showed; absent when none did. */
  headerMs?: number;
  /** Until its diff finished loading or failed; absent when it didn't. */
  diffMs?: number;
  /** Until the patch read started; absent when the page already held it. */
  patchRequestedMs?: number;
  /** Until the Changes tab showed; absent when it never did. */
  changesShownMs?: number;
  /** Until Changes showed with its patch answered, after that render; absent when it never did. */
  diffShownMs?: number;
}

/** The report field of each milestone past the panel's state. */
const MILESTONE_FIELDS = {
  header: "headerMs",
  patchRequested: "patchRequestedMs",
  diff: "diffMs",
  changesShown: "changesShownMs",
  diffShown: "diffShownMs",
} as const satisfies Record<Exclude<PullRequestTimingMilestone, "state">, string>;

/** Durations past this are a parked or suspended window, not a load. */
const MAX_DURATION_MS = 10 * 60_000;

/** One opening of the panel for one session, from its first info request. */
export class PullRequestTiming {
  private readonly marks = new Map<PullRequestTimingMilestone, number>();
  private outcome: string | null = null;
  private reported = false;
  // Whether Changes shows now; its first showing is kept apart, as the `changesShown` mark.
  private changesShowing = false;

  private readonly sessionId: string;
  private readonly path: PullRequestReadPath;
  private readonly cached: boolean;
  private readonly startedAt: number;

  constructor(sessionId: string, path: PullRequestReadPath, cached: boolean, startedAt: number) {
    this.sessionId = sessionId;
    this.path = path;
    this.cached = cached;
    this.startedAt = startedAt;
  }

  /**
   * Record the first time a milestone is reached; later repeats are ignored. Any
   * order is accepted: a child view's effect can report its header before the
   * panel's own effect marks the state in the same render.
   */
  mark(
    milestone: Exclude<PullRequestTimingMilestone, "changesShown">,
    at: number,
    outcome?: string,
  ): void {
    // A state without its outcome is ignored, so a later mark can still name it.
    if (milestone === "state" && !outcome) return;
    if (this.record(milestone, at) && milestone === "state") this.outcome = outcome ?? null;
  }

  /** Changes started or stopped showing; its first showing is the `changesShown` milestone. */
  showChanges(showing: boolean, at: number): void {
    if (this.reported) return;
    this.changesShowing = showing;
    if (showing) this.record("changesShown", at);
  }

  /** Whether this was the milestone's first time. */
  private record(milestone: PullRequestTimingMilestone, at: number): boolean {
    if (this.reported || this.marks.has(milestone)) return false;
    this.marks.set(milestone, at);
    return true;
  }

  /**
   * Whether this opening has nothing more to measure: a whole-panel state, or the diff read.
   * With Changes showing now, it waits for the diff to show there too, which the user waits on.
   */
  get complete(): boolean {
    if (!this.marks.has("state")) return false;
    if (this.outcome !== "ready") return true;
    if (!this.marks.has("diff")) return false;
    return !this.changesShowing || this.marks.has("diffShown");
  }

  /**
   * The report, once, or null before the panel left loading, when already
   * reported, or when a duration is out of range.
   */
  takeReport(): PullRequestTimingReport | null {
    if (this.reported) return null;
    const stateMs = this.duration("state");
    if (stateMs === null || this.outcome === null) return null;
    this.reported = true;
    const report: PullRequestTimingReport = {
      sessionId: this.sessionId,
      path: this.path,
      outcome: this.outcome,
      cached: this.cached,
      stateMs,
    };
    for (const [milestone, field] of Object.entries(MILESTONE_FIELDS)) {
      const elapsed = this.duration(milestone as keyof typeof MILESTONE_FIELDS);
      if (elapsed !== null) report[field] = elapsed;
    }
    return report;
  }

  private duration(milestone: PullRequestTimingMilestone): number | null {
    const at = this.marks.get(milestone);
    if (at === undefined) return null;
    const elapsed = Math.round(at - this.startedAt);
    return elapsed >= 0 && elapsed <= MAX_DURATION_MS ? elapsed : null;
  }
}
