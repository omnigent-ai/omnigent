import { stripBasePath } from "./basePath";
import { TEMP_CONV_ID_PREFIX } from "./tempConversationId";

// A project file opens in the session FileViewer under a client-only scope ID.
// The `temp:` prefix keeps session-bound hooks from calling session APIs for it.
const PROJECT_FILE_SCOPE_PREFIX = `${TEMP_CONV_ID_PREFIX}project-file:`;

export interface ProjectFileScope {
  projectId: string;
  fileId: string;
}

export function projectFileScopeId(projectId: string, fileId: string): string {
  return `${PROJECT_FILE_SCOPE_PREFIX}${projectId}:${fileId}`;
}

export function parseProjectFileScope(id: string | null | undefined): ProjectFileScope | null {
  if (!id?.startsWith(PROJECT_FILE_SCOPE_PREFIX)) return null;
  const [projectId, fileId] = id.slice(PROJECT_FILE_SCOPE_PREFIX.length).split(":");
  return projectId && fileId ? { projectId, fileId } : null;
}

export function projectFileApiPath({ projectId, fileId }: ProjectFileScope): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`;
}

/** The comments API base for a session, or for a project file's scope ID. */
export function commentsApiBase(scopeId: string): string {
  const projectFile = parseProjectFileScope(scopeId);
  return projectFile
    ? projectFileApiPath(projectFile)
    : `/v1/sessions/${encodeURIComponent(scopeId)}`;
}

/** Parse `/v1/projects/{project}/files/{file}/content`, the `artifact` ticket output form. */
export function parseProjectFileContentPath(href: string): ProjectFileScope | null {
  let pathname: string;
  try {
    pathname = new URL(href, window.location.origin).pathname;
  } catch {
    return null;
  }
  const match = /\/v1\/projects\/([^/]+)\/files\/([^/]+)\/content$/.exec(pathname);
  return match
    ? { projectId: decodeURIComponent(match[1]), fileId: decodeURIComponent(match[2]) }
    : null;
}

/** A project file or ticket attachment named by its `.../content` URL. */
export interface ProjectContentTarget extends ProjectFileScope {
  kind: "file" | "attachment";
  /** The same-origin API path to fetch it with the viewer's credentials. */
  contentPath: string;
}

const VIDEO_ATTACHMENT_MARKER = "?type=video";

// `/v1/projects/{project}/files/{id}/content` or `.../ticket-attachments/{id}/content`.
export const PROJECT_CONTENT_PATH =
  /^\/v1\/projects\/([^/?#]+)\/(files|ticket-attachments)\/([^/?#]+)\/content$/;

/** Parse a same-origin project file or ticket-attachment content link. */
export function parseProjectContentLink(href: string): ProjectContentTarget | null {
  let url: URL;
  try {
    url = new URL(href, window.location.origin);
  } catch {
    return null;
  }
  // Ticket attachments mark a video with exactly `?type=video`; nothing else may ride along.
  if (url.origin !== window.location.origin || url.hash) return null;
  if (url.search && url.search !== VIDEO_ATTACHMENT_MARKER) return null;
  const pathname = stripBasePath(url.pathname);
  const match = PROJECT_CONTENT_PATH.exec(pathname);
  if (url.search && match?.[2] !== "ticket-attachments") return null;
  if (!match) return null;
  try {
    return {
      projectId: decodeURIComponent(match[1]),
      fileId: decodeURIComponent(match[3]),
      kind: match[2] === "files" ? "file" : "attachment",
      contentPath: pathname,
    };
  } catch {
    return null;
  }
}
