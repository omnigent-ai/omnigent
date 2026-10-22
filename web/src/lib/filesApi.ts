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
