import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** An agent installed with {@link installAgentBundle}. */
export interface InstalledAgent {
  id: string;
  name: string;
}

/**
 * Install (or replace) the caller's reusable agent from a `.tar.gz` bundle,
 * the same shape `omnigent agent add` uploads. The server validates the
 * bundle and never runs it; the agent then stays in the new-session picker.
 *
 * @param bundle - Gzipped tarball chosen by the user.
 * @returns The installed agent's id and name.
 * @throws Error carrying the server's message when the install is rejected.
 */
export async function installAgentBundle(bundle: File): Promise<InstalledAgent> {
  const form = new FormData();
  form.append("bundle", bundle, bundle.name);
  const res = await authenticatedFetch("/v1/agents", { method: "POST", body: form });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json().catch(() => null)) as Partial<InstalledAgent> | null;
  if (typeof body?.id !== "string" || typeof body.name !== "string") {
    throw new Error("Unexpected response from the server when installing the agent.");
  }
  return { id: body.id, name: body.name };
}
