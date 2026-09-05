import { createProject, listProjects, type Project } from "@/lib/projectsApi";
import type { ExtensionProjectSummary } from "../types";
import { ExtensionHostServiceError } from "./errors";

export const PROJECT_NAME_MAX_LENGTH = 100;
export const PROJECT_ICON_MAX_LENGTH = 16;
const PROJECT_ID_MAX_LENGTH = 256;
const PROJECT_ERROR_MAX_LENGTH = 200;

export function projectSummary(project: Project): ExtensionProjectSummary {
  const { id, name } = project;
  if (typeof id !== "string" || id.length < 1 || id.length > PROJECT_ID_MAX_LENGTH) {
    throw new ExtensionHostServiceError("HostError", "Project id is malformed");
  }
  if (typeof name !== "string") {
    throw new ExtensionHostServiceError("HostError", "Project name is malformed");
  }
  const icon = project.config?.icon;
  return {
    id,
    name: name.slice(0, PROJECT_NAME_MAX_LENGTH),
    icon:
      typeof icon === "string" && icon.length > 0 ? icon.slice(0, PROJECT_ICON_MAX_LENGTH) : null,
  };
}

export function parseCreateProjectParams(params: unknown): string {
  if (!params || typeof params !== "object" || Array.isArray(params)) {
    throw new ExtensionHostServiceError("InvalidParams", "Expected an object");
  }
  const name = (params as Record<string, unknown>).name;
  if (typeof name !== "string") {
    throw new ExtensionHostServiceError("InvalidParams", "name is required");
  }
  const trimmed = name.trim();
  if (trimmed.length < 1 || trimmed.length > PROJECT_NAME_MAX_LENGTH) {
    throw new ExtensionHostServiceError(
      "InvalidParams",
      `name must be 1 to ${PROJECT_NAME_MAX_LENGTH} characters`,
    );
  }
  return trimmed;
}

export async function listProjectSummaries(): Promise<ExtensionProjectSummary[]> {
  let projects: Project[];
  try {
    projects = await listProjects();
  } catch {
    throw new ExtensionHostServiceError("HostError", "Project list request failed");
  }
  return projects.map(projectSummary);
}

export async function createProjectSummary(name: string): Promise<ExtensionProjectSummary> {
  let project: Project;
  try {
    project = await createProject(name);
  } catch (error) {
    const detail = error instanceof Error ? error.message.slice(0, PROJECT_ERROR_MAX_LENGTH) : "";
    throw new ExtensionHostServiceError(
      "HostError",
      detail ? `Project could not be created: ${detail}` : "Project could not be created",
    );
  }
  return projectSummary(project);
}
