import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export type ProjectSlackState = "claimed" | "active" | "disabled" | "error" | "needs_reconnect";

/** A project's Slack channel link as Project settings shows it. */
export interface ProjectSlackLink {
  object: "project.slack";
  linked: boolean;
  can_manage: boolean;
  id?: string;
  slack_team_id?: string | null;
  channel_id?: string;
  channel_name?: string | null;
  state?: ProjectSlackState;
  destination?: Record<string, string>;
  automation_id?: string | null;
  last_error?: string | null;
  owner_user_id?: string;
  updated_by_user_id?: string;
  updated_at?: number;
  overlaps?: string[];
}

export interface ProjectSlackLinkInput {
  channel_link?: string;
  slack_team_id?: string;
  channel_id?: string;
  channel_name?: string;
}

function slackPath(projectId: string, suffix = ""): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/slack${suffix}`;
}

async function readLink(res: Response): Promise<ProjectSlackLink> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectSlackLink;
}

export async function getProjectSlack(projectId: string): Promise<ProjectSlackLink> {
  return readLink(await authenticatedFetch(slackPath(projectId)));
}

export async function linkProjectSlack(
  projectId: string,
  input: ProjectSlackLinkInput,
): Promise<ProjectSlackLink> {
  return readLink(
    await authenticatedFetch(slackPath(projectId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function setProjectSlackEnabled(
  projectId: string,
  enabled: boolean,
): Promise<ProjectSlackLink> {
  return readLink(
    await authenticatedFetch(slackPath(projectId, enabled ? "/enable" : "/disable"), {
      method: "POST",
    }),
  );
}

export async function unlinkProjectSlack(projectId: string): Promise<ProjectSlackLink> {
  return readLink(await authenticatedFetch(slackPath(projectId), { method: "DELETE" }));
}
