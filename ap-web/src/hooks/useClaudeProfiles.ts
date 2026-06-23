import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * A configured Claude Code account profile, surfaced read-only from the
 * operator's `claude_profiles` block via `GET /v1/claude-profiles`.
 *
 * The server returns only the name + display label — never the
 * config-dir path or any credential — so this type carries nothing
 * sensitive. `name` is the value sent back as `claude_profile` on
 * `POST /v1/sessions`; `display` is the picker label.
 */
export interface ClaudeProfile {
  name: string;
  display: string;
}

interface ClaudeProfileWire {
  name: string;
  display: string;
}

/**
 * Fetch the operator's configured Claude Code account profiles. Empty
 * list when the server has no `claude_profiles` block (single-account
 * deployments) or the endpoint is unreachable — the picker hides
 * itself in that case, so a failed fetch must not throw to the UI.
 */
async function fetchClaudeProfiles(): Promise<ClaudeProfile[]> {
  const res = await authenticatedFetch("/v1/claude-profiles");
  // Defensive against a non-Response (e.g. a stubbed fetch in tests) or a
  // non-OK reply — the picker hides itself on an empty list, so a failed
  // fetch must never throw to the UI.
  if (!res || !res.ok) return [];
  const data = (await res.json()) as { data?: ClaudeProfileWire[] };
  return (data.data ?? []).map((p) => ({ name: p.name, display: p.display }));
}

export function useClaudeProfiles() {
  return useQuery({
    queryKey: ["claude-profiles"],
    queryFn: fetchClaudeProfiles,
    staleTime: 60_000,
  });
}
