// Names shared by the automation dialog and the sidebar for where automation
// runs are filed, and the server-owned labels that mark a run's sessions.

/** The project a new automation files its runs into unless its owner picks another. */
export const DEFAULT_AUTOMATION_PROJECT = "Omnigent Automation";

/** The automation whose run created the session; forks keep it. */
export const SCHEDULED_TASK_ORIGIN_LABEL_KEY = "omnigent.scheduled_task_origin";
/** Set on the session a run created itself; forks drop it. */
export const SCHEDULED_TASK_INITIAL_RUN_LABEL_KEY = "omnigent.scheduled_task_initial_run";
/** The automation whose run's session tree launched the session; forks drop it. */
export const SCHEDULED_TASK_LAUNCH_LABEL_KEY = "omnigent.scheduled_task_launch";
