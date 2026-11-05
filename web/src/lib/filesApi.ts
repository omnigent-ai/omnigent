import { attachmentFilename } from "./attachments";
import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface UploadedFile {
  id: string;
  filename: string;
  bytes: number;
  created_at: number;
}

export async function uploadFile(sessionId: string, file: File): Promise<UploadedFile> {
  const form = new FormData();
  const filename = attachmentFilename(file);
  form.append("file", file, filename);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files`,
    {
      method: "POST",
      body: form,
    },
  );
  // Carry the server's reason ("Unsupported attachment type 'application/zip'…"
  // for 415, the size cap for 413) rather than a bare status number, so the
  // failure tells the user what to do about it.
  if (!res.ok) throw await apiErrorFromResponse(res);
  const resource = (await res.json()) as {
    id: string;
    name?: string;
    metadata?: {
      filename?: string;
      bytes?: number;
      created_at?: number;
    };
  };
  return {
    id: resource.id,
    filename: resource.metadata?.filename ?? resource.name ?? filename,
    bytes: resource.metadata?.bytes ?? file.size,
    created_at: resource.metadata?.created_at ?? 0,
  };
}

/**
 * Download a session's uploaded file back into a `File`, e.g. to attach it again.
 *
 * @param sessionId - The session the file was uploaded to.
 * @param fileId - The file's id from its message block.
 * @param filename - Name for the returned file, e.g. ``"chart.png"``.
 * @throws ApiError when the server refuses the download.
 */
export async function downloadSessionFile(
  sessionId: string,
  fileId: string,
  filename: string,
): Promise<File> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(fileId)}/content`,
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const blob = await res.blob();
  // Its bytes, not the Blob itself: a File from another implementation (e.g. a test
  // DOM's) doesn't recognize the fetch Blob and stores "[object Blob]".
  return new File([await blob.arrayBuffer()], filename, { type: blob.type });
}
