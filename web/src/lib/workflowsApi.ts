import { authenticatedFetch } from "./identity";

export type WorkflowBackend = "simulation" | "arclet";
export interface WorkflowOptions {
  backends: { id: WorkflowBackend; enabled: boolean; reason: string | null; example: string }[];
}
export interface WorkflowTask {
  id: string;
  name?: string | null;
  goal: string;
  depends_on: string[];
  gates: { id: string; kind: "external"; artifacts: string[] }[];
  allow_expansion?: boolean;
  max_rounds?: number;
}
export interface WorkflowMember {
  id: string;
  role: string;
  agent_id: string;
  placement: { kind: "sandbox"; provider: "arclet"; workspace?: string };
}
export interface WorkflowTeam {
  id: string;
  lead: string;
  strategy: "owner" | "parallel";
  members: WorkflowMember[];
}
export interface WorkflowDefinition {
  name: string;
  goal: string;
  teams: WorkflowTeam[];
  nodes: (WorkflowTask & { team: string })[];
}
interface Artifact {
  id: string;
  kind: string;
  uri: string;
  revision: string;
}
interface Candidate {
  summary: string;
  status: string;
  artifacts: Artifact[];
  outputs?: Record<string, unknown>;
}
interface Member {
  session_id: string | null;
  cleanup_error: string | null;
  turns: Record<string, { status: string; remote_id: string | null }>;
}
export interface WorkflowRun {
  run_id: string;
  status: string;
  active: boolean;
  controller_error: string | null;
  binding: { mode: WorkflowBackend; server: string | null };
  workflow: WorkflowDefinition;
  nodes: Record<
    string,
    {
      status: string;
      round: number;
      summary: string | null;
      error: string | null;
      candidate_id: string | null;
      candidate: Candidate | null;
      candidates: Record<string, Candidate>;
      artifacts: Artifact[];
      outputs?: Record<string, unknown>;
      members: Record<string, Member>;
      gate_results: {
        gate_id: string;
        candidate_id: string;
        outcome: { passed: boolean; detail: string; evidence: string | null };
      }[];
    }
  >;
  events: { event: { id: string }; status: string; error: string | null }[];
}
export async function workflowRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await authenticatedFetch(
    "/v1/workflows" + path,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail
              .map(
                (error: { loc: (string | number)[]; msg: string }) =>
                  `${error.loc.slice(1).join(" → ")}: ${error.msg}`,
              )
              .join("\n")
          : (data.message ?? "Workflow request failed"),
    );
  }
  return data as T;
}
