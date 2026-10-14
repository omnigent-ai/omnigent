import { authenticatedFetch } from "./identity";

export interface UploadLimits {
  max_file_bytes: number;
  chunk: { min: number; initial: number; max: number };
  max_files_per_batch: number;
  max_batch_bytes: number;
  max_open_uploads: number;
  max_open_bytes: number;
  parallel_files: number;
}

export interface UploadCapability {
  supported: boolean;
  reason?: string;
  limits: UploadLimits;
}

export interface UploadOwner {
  origin: string;
  identity: string;
  session: string;
  environment: string;
}

export interface UploadResult {
  path: string;
  size: number;
  sha256: string;
  outcome: "created" | "replaced" | "renamed";
  final_name: string;
}

export class UploadError extends Error {
  readonly status: number;
  readonly code: string;
  readonly headers: Headers;
  constructor(message: string, status: number, code: string, headers: Headers) {
    super(message);
    this.status = status;
    this.code = code;
    this.headers = headers;
  }
}

export function uploadUrl(owner: UploadOwner, suffix = ""): string {
  return `/v1/sessions/${encodeURIComponent(owner.session)}/resources/environments/${encodeURIComponent(owner.environment)}/uploads${suffix}`;
}

export async function uploadRequest(
  owner: UploadOwner,
  suffix = "",
  init?: RequestInit,
): Promise<Response> {
  const response = await authenticatedFetch(uploadUrl(owner, suffix), init);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: { code?: string; message?: string };
    } | null;
    throw new UploadError(
      body?.error?.message ?? `Upload request failed (${response.status})`,
      response.status,
      body?.error?.code ?? "unknown",
      response.headers,
    );
  }
  return response;
}

export async function getUploadCapability(owner: UploadOwner): Promise<UploadCapability | null> {
  try {
    return (await (await uploadRequest(owner)).json()) as UploadCapability;
  } catch (error) {
    if (error instanceof UploadError && error.status === 404) return null;
    throw error;
  }
}

export function acknowledgedOffset(response: Response | UploadError, size: number): number {
  const raw = response.headers.get("Upload-Offset");
  const offset = raw !== null && /^\d+$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > size) {
    throw new Error("Invalid upload acknowledgement");
  }
  return offset;
}
