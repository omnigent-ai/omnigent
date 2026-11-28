// How a workspace read reports that the session's runner isn't connected. The workspace hooks
// own these, so a read that throws one and the retry policy that checks it share the class.

export { isRunnerUnavailable503, RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
